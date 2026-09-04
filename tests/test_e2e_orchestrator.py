"""End-to-end orchestrator spine against a real bare repo, with a fake backend.

Proves the deterministic pipeline (branch -> commit -> tamper guard -> tests ->
push -> open local PR -> awaiting_approval) without spending LLM quota. A second
case proves the tamper guard blocks a test-weakening change and escalates.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from types import SimpleNamespace as _SimpleNamespace

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.cli.commands import _review_pass_evidence
from no_human.config import load_config
from no_human.core.orchestrator import (
    Orchestrator, _REFORMAT_NUDGE, _REFORMAT_NUDGE_MARKER,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo, PrResult
from no_human.review.reviewer import AdversarialReviewer, ReviewDecision
from no_human.review.selfcheck import ChecklistItem
from no_human.testing.repro_gate import MANIFEST as REPRO_MANIFEST


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_out(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    # a product file + an existing test, so the tamper guard has a baseline
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


class FakeBackend:
    """Stands in for ClaudeBackend: applies a scripted file mutation."""

    def __init__(self, mutate):
        self.mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    # Disable planning by default in tests — no real Claude calls.
    # Planning-specific tests override this and mock ClaudeBackend.
    cfg.data.setdefault("planning", {})["enabled"] = False
    # These tests exercise the pipeline around the review gate, not the gate
    # itself, and most construct an Orchestrator with no reviewer. In
    # production that now escalates (the gate fails closed); here the skip is
    # deliberate and must be stated. The gate's own behaviour is covered by
    # tests/test_review_fail_closed.py.
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    # The escalation-quality gate (blockers.challenge) is ON in production; OFF
    # here so every blocker test pins the park semantics it was written for
    # WITHOUT a live supervisor call (same deliberate-and-stated pattern as
    # allow_advisory above). The gate's own behavior is covered by
    # tests/test_blocker_challenge.py with the advisory seam patched.
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


async def test_full_pipeline_opens_local_pr(bare_repo, tmp_path, store):
    def mutate(cwd):
        # add a real feature + a real test (no tampering)
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url and "no-human/" in outcome.pr_url
    # branch pushed to the bare remote
    branches = subprocess.run(["git", "branch", "--list"], cwd=bare_repo,
                              capture_output=True, text=True).stdout
    assert "no-human/" in branches
    # attempt recorded with a PR + passing tests
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["pr_url"] == outcome.pr_url
    assert attempts[-1]["status"] == "succeeded"
    kinds = [e["kind"] for e in events]
    assert "pr_open" in kinds and "commit" in kinds
    # CI.5: no remote CI configured for this repo -> the pipeline honestly
    # surfaces that the PR was gated on local tests only (not a remote-CI pass).
    assert "ci_skipped" in kinds
    ci_skipped = [e for e in events if e["kind"] == "ci_skipped"]
    assert ci_skipped and ci_skipped[0].get("remote_ci") is False


async def test_receipt_survives_main_moving_after_the_push(
        bare_repo, tmp_path, store, monkeypatch):
    """Live incident abc7e570: a correctly-opened PR was reported 'lost'
    because the receipt check compared the PR head against a re-resolved
    `repo.head_sha()`. By the time that ran, main had moved in the SAME
    working tree (a human merged something else while this task waited) —
    HEAD no longer pointed at the pushed branch's tip, so a correct delivery
    read as lost. The receipt must compare against the SHA that was actually
    pushed, captured at push time — not HEAD re-resolved later."""
    from no_human.core import orchestrator as orch_mod

    real_open_pr = orch_mod.open_pr
    real_verify = orch_mod.verify_pr_receipt
    captured = {}

    def spy_open_pr(repo, branch, title, body, **kwargs):
        result = real_open_pr(repo, branch, title, body, **kwargs)
        captured["pushed_sha"] = result.pushed_sha
        # Independent source of truth: the branch tip read directly via
        # `git rev-parse <branch>`, NOT anything the code under test
        # returned. This is what makes the check below non-tautological.
        captured["branch_tip"] = _git_out(repo.path, "rev-parse", branch)
        # main moves in the SAME working tree after the push landed — e.g. a
        # human merged an unrelated PR while this task waited on CI/review.
        # repo.head_sha() now reads main's tip, not the pushed branch's head.
        _git(repo.path, "checkout", "main")
        (repo.path / "unrelated.md").write_text("docs\n")
        _git(repo.path, "add", "-A")
        _git(repo.path, "commit", "-m", "unrelated docs commit")
        return result

    def spy_verify(repo_path, pr_url, *, expected_files, local_sha,
                   committed_files=None, runner=None):
        captured["local_sha"] = local_sha

        # Stand in for the forge: it genuinely reports the pushed branch's
        # head — the PR is correct, the defect is in the comparand, not
        # the forge's view.
        def fake_runner(args, **kw):
            class P:
                returncode = 0
                stdout = json.dumps(
                    {"files": [], "headRefOid": captured["pushed_sha"]})
                stderr = ""
            return P()

        return real_verify(repo_path, pr_url, expected_files=expected_files,
                           local_sha=local_sha, committed_files=committed_files,
                           runner=fake_runner)

    monkeypatch.setattr(orch_mod, "open_pr", spy_open_pr)
    monkeypatch.setattr(orch_mod, "verify_pr_receipt", spy_verify)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    cfg.data["isolation"]["enabled"] = False   # repo IS bare_repo; no linked
                                               # worktree, so "main" can be
                                               # checked out in the same path.
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert captured["local_sha"] == captured["pushed_sha"], (
        "local_sha passed to verify_pr_receipt must be the SHA captured at "
        "push time, not a HEAD re-resolved after main moved")
    # Independent checks against the git-tracking-message regression: a real
    # SHA, and one that matches the branch tip read separately (not merely
    # threaded consistently through the code under test).
    assert _SHA_RE.match(captured["pushed_sha"]), (
        f"pushed_sha is not a 40-char lowercase-hex SHA: {captured['pushed_sha']!r}")
    assert captured["pushed_sha"] == captured["branch_tip"], (
        "pushed_sha must equal the branch tip read independently via "
        f"`git rev-parse`: {captured['pushed_sha']!r} != {captured['branch_tip']!r}")
    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "succeeded"
    receipts = [e for e in events if e["kind"] == "receipt"]
    assert receipts and receipts[-1]["status"] == "landed", receipts


async def test_receipt_still_lost_on_a_genuine_sha_mismatch(
        bare_repo, tmp_path, store, monkeypatch):
    """Negative control: the fix must not weaken the check into a warning.
    When the forge's PR head genuinely differs from what was pushed (a real
    force-push/squash divergence), the receipt must still report 'lost', and
    the message must name which refs were compared."""
    from no_human.core import orchestrator as orch_mod

    real_verify = orch_mod.verify_pr_receipt

    def spy_verify(repo_path, pr_url, *, expected_files, local_sha,
                   committed_files=None, runner=None):
        def fake_runner(args, **kw):
            class P:
                returncode = 0
                stdout = json.dumps(
                    {"files": [], "headRefOid": "deadbeef0000000000000000000000000000dead"})
                stderr = ""
            return P()
        return real_verify(repo_path, pr_url, expected_files=expected_files,
                           local_sha=local_sha, committed_files=committed_files,
                           runner=fake_runner)

    monkeypatch.setattr(orch_mod, "verify_pr_receipt", spy_verify)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    receipts = [e for e in events if e["kind"] == "receipt"]
    assert receipts and receipts[-1]["status"] == "lost", receipts
    detail = receipts[-1]["text"]
    assert "head" in detail.lower() and "pushed" in detail.lower(), detail
    assert outcome.status is not TaskStatus.AWAITING_APPROVAL, outcome.detail


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_repro_gate_runs_advisory_inside_the_pipeline(bare_repo, tmp_path, store):
    """The coder declares its demonstrating test; the gate proves both
    directions and emits its verdict — without changing the outcome."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        (cwd / ".no_human").mkdir(exist_ok=True)
        (cwd / REPRO_MANIFEST).write_text(
            '{"tests": ["test_calc.py::test_mul"]}'
        )

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    gate = [e for e in events if e["kind"] == "repro_gate"]
    assert len(gate) == 1
    assert gate[0]["verdict"] == "pass", gate[0]
    # test_mul fails on the base (no mul) and passes after — a true repro.


async def test_repro_gate_waives_loudly_without_a_manifest(bare_repo, tmp_path, store):
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    gate = [e for e in events if e["kind"] == "repro_gate"]
    assert len(gate) == 1 and gate[0]["verdict"] == "waived"


async def test_transient_pr_open_failure_retries_instead_of_escalating(
        bare_repo, tmp_path, store, monkeypatch):
    """Live incident: `gh pr create` returned an EOF after a successful push
    and the task escalated as if a human were needed. One retry must absorb
    it (open_pr is idempotent on the forges we target)."""
    from no_human.core import orchestrator as orch_mod

    real_open_pr = orch_mod.open_pr
    calls = {"n": 0}

    def flaky_open_pr(repo, branch, title, body, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gh pr create failed: unexpected EOF")
        return real_open_pr(repo, branch, title, body, **kwargs)

    async def no_sleep(_secs):
        return None

    monkeypatch.setattr(orch_mod, "open_pr", flaky_open_pr)
    monkeypatch.setattr(orch_mod.asyncio, "sleep", no_sleep)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert calls["n"] == 2
    kinds = [e["kind"] for e in events]
    assert "pr_open_retry" in kinds and "pr_open" in kinds


async def _run_on_stale_checkout(tmp_path, store, monkeypatch, *, pinned_base=None,
                                 drop_local_default=False):
    """Run a task in a repo whose CHECKOUT is parked on 'master' while the
    remote's real default is 'main'. Returns (outcome, base passed to open_pr,
    the task as stored, events)."""
    from no_human.core import orchestrator as orch_mod

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")  # establishes real refs/heads/main
    _git(work, "checkout", "-b", "master")  # stale local checkout, mismatched
    if drop_local_default:
        # The F1 shape: the derived default has NO local ref (single-branch
        # clone / renamed remote default / typo'd profile) — only origin/main
        # can seed the worktree.
        _git(work, "branch", "-D", "main")

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    captured = {}
    real_open_pr = orch_mod.open_pr

    def spy_open_pr(repo, branch, title, body, **kwargs):
        captured["base"] = kwargs.get("base")
        return real_open_pr(repo, branch, title, body, **kwargs)

    monkeypatch.setattr(orch_mod, "open_pr", spy_open_pr)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(work))
    if pinned_base:
        t.context = {**(t.context or {}), "base_branch": pinned_base}
    await store.create_task(t)

    outcome = await orch.run_task(t)
    return outcome, captured.get("base"), await store.get_task(t.id), events


async def test_pr_base_is_the_default_branch_not_the_stale_checkout_branch(
        tmp_path, store, monkeypatch):
    """The live defect (2026-08-09, reference trace task 0960f3a9): the
    operator's checkout was parked on a stale local branch, the base was taken
    from it, and `gh pr create` refused twice — "No commits between
    land/term-gate and no-human/<id>", "Base ref must be a branch" — before the
    task escalated NOVEL_UNKNOWN. The product warned first and proceeded on the
    wrong base anyway, which is what this pins: the checkout is on 'master',
    the remote's default is 'main', and the PR must target 'main'.

    This test used to assert the WARNING (C3's auto-detect) and accept the
    wrong base. The auto-detect it covered is still covered — it is what
    resolves 'main' here, with no ProjectProfile.default_branch ever confirmed
    — but the outcome it now demands is the right base, not a note about the
    wrong one."""
    outcome, base, stored, events = await _run_on_stale_checkout(
        tmp_path, store, monkeypatch)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert base == "main", f"PR opened against {base!r}, not the default branch"
    assert stored.context["base_branch"] == "main", (
        "the persisted base must be the default branch, so a resume/wake "
        "re-derives nothing from the checkout either")
    mismatch = [e for e in events
                if e.get("kind") == "warning"
                and "differs from project default_branch" in e["text"]]
    assert not mismatch, (
        f"no mismatch warning is due when the base IS the default: {mismatch}")


async def test_a_default_branch_with_no_local_ref_still_runs_and_targets_it(
        tmp_path, store, monkeypatch):
    """Review finding F1, empirically proven on the first cut: when 'main' has
    no LOCAL ref (single-branch clone, renamed remote default, typo'd profile)
    the task hard-failed at `git worktree add ... main` with remediation text
    blaming isolation.worktree_root. The branch point must fall through to
    origin/main while the PR base stays 'main'."""
    outcome, base, stored, events = await _run_on_stale_checkout(
        tmp_path, store, monkeypatch, drop_local_default=True)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, (
        f"the task must run, not die at worktree creation: {outcome}")
    assert base == "main", f"PR opened against {base!r}, not the default branch"
    assert stored.context["base_branch"] == "main"


async def test_an_explicitly_pinned_base_still_wins_and_warns(
        tmp_path, store, monkeypatch):
    """The implicit inheritance dies; explicit bases do not. A pinned base
    (the API's `base_branch`, PR-001) and the lead agent's stacked-PR chaining
    (`_unblock_ready` propagates a dependency's PR branch) both arrive as
    `context["base_branch"]`, and both must still target exactly that branch —
    with the mismatch warning, which now means "explicit override" rather than
    "about to fail"."""
    outcome, base, stored, events = await _run_on_stale_checkout(
        tmp_path, store, monkeypatch, pinned_base="master")

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert base == "master", f"the pinned base was overridden with {base!r}"
    assert stored.context["base_branch"] == "master"
    warnings = [e for e in events if e.get("kind") == "warning"]
    assert any("master" in w["text"] and "main" in w["text"] for w in warnings), (
        f"expected a default-branch mismatch warning, got: {[e['text'] for e in warnings]}"
    )


async def test_run_task_uses_confirmed_profile_test_command(bare_repo, tmp_path, store):
    """A usable ProjectProfile's proven test_cmd drives the run, not detect_command."""
    from no_human.profile import ProjectProfile

    marker = bare_repo / ".profile_ran"

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    # A profile whose test command writes a sentinel — distinct from `pytest -q`,
    # so its presence proves the profile (not the heuristic) chose the command.
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="custom",
        test_cmd=f"sh -c 'echo ran > {marker}; exit 0'",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert marker.exists(), "profile test_cmd did not run"
    assert "profile" in [e["kind"] for e in events]


class _GatedCI:
    """A CI backend that must be started by a human (like JenkinsCI)."""
    name = "jenkins"
    max_infra_retries = 0

    async def trigger(self, branch, extra_variables=None):
        from no_human.ci.base import HumanGatedCI
        raise HumanGatedCI("build it first", wake_hint="Build image on Jenkins job X")


class _RecordingNotifier(SlackNotifier):
    def __init__(self):
        super().__init__(None)
        self.sent = []

    def notify(self, kind, message):
        self.sent.append((kind, message))


def _feature_mutate(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n")


async def test_human_gated_ci_parks_with_wake_and_notifies(bare_repo, tmp_path, store):
    cfg = _config(tmp_path)
    notifier = _RecordingNotifier()
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_feature_mutate), notifier,
                        event_sink=events.append, ci_runner=_GatedCI())
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.BLOCKED          # parked, not failed/infra
    t = await store.find_task(t.id)
    assert t.blocker["category"] == "DEPENDENCY_WAIT"
    assert t.blocker["wake_condition"].startswith("ci_green_on:no-human/")
    assert t.context["human_gated_ci"]["branch"].startswith("no-human/")
    # the human got a heads-up (parked-but-actionable), and the branch is pushed.
    assert notifier.sent and "Jenkins" in notifier.sent[-1][1]
    branches = subprocess.run(["git", "branch", "--list"], cwd=bare_repo,
                              capture_output=True, text=True).stdout
    assert "no-human/" in branches
    # no PR opened yet — CI hasn't verified the change.
    attempts = await store.list_attempts(t.id)
    assert all(not a.get("pr_url") for a in attempts)


async def test_human_gated_ci_resume_opens_pr_without_rerunning_agent(
    bare_repo, tmp_path, store
):
    cfg = _config(tmp_path)
    # First run: park on the gate. Give it a real (fake) passing reviewer so a
    # genuine review round stamps `review_history` with this commit's sha
    # before parking — advisory (no-reviewer) passes deliberately do NOT
    # stamp (see orchestrator.py `_run_review`'s advisory branch), and the
    # delivery-sha gate (`_assert_delivery_sha`) fails closed on a resume
    # that crosses a fresh `Orchestrator` instance with nothing stamped, by
    # design: an advisory pass means "nothing was reviewed", so there is
    # nothing honest to accept after a restart. A real stamped PASS is what
    # this test's resume path is actually meant to exercise.
    passing_decision = ReviewDecision(
        passed=True,
        checklist=[
            ChecklistItem("mul(a,b) implemented", True, "calc.py:3 returns a*b"),
            ChecklistItem("tests added", True, "test_calc.py:5 test_mul asserts mul(2,3)==6"),
        ],
    )
    reviewer = FakeReviewer(passing_decision)
    orch = Orchestrator(store, cfg.data, FakeBackend(_feature_mutate),
                        SlackNotifier(None), ci_runner=_GatedCI(), reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)
    assert (await orch.run_task(t)).status is TaskStatus.BLOCKED

    # Human/watcher resumes (gate cleared). The agent must NOT run again, and the
    # gated CI must NOT be re-triggered — we go straight to the PR.
    class _Exploding:
        async def run(self, *a, **k):
            raise AssertionError("agent must not re-run on a human-gated resume")

    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    t = await store.find_task(t.id)
    orch2 = Orchestrator(store, cfg.data, _Exploding(), SlackNotifier(None),
                         ci_runner=_GatedCI())

    outcome = await orch2.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url and "no-human/" in outcome.pr_url
    t = await store.find_task(t.id)
    assert "human_gated_ci" not in (t.context or {})
    assert t.blocker in (None, {})


async def test_tamper_weakening_is_blocked_and_stops_the_run(bare_repo, tmp_path, store):
    """The documented reward hack — gut the test so the broken code goes green —
    must not ship, and the run must stop on a human.

    WHAT CHANGED ON 2026-08-09, and what did not. The terminal state was
    ESCALATED, because every tamper fire escalated. Now a fire is adjudicated
    first (`_handle_tamper_fire`), and this Orchestrator is built with NO
    reviewer, so the adjudication cannot run — which is CANNOT_DECIDE, which
    parks the task for a human with the plain-language card. The safety
    property is byte-for-byte what it was: nothing shipped, no PR, the run
    ended, a human holds it, and the attempt row says why. Only WHICH
    human-held state it lands in moved, and only because the product now asks
    a question instead of filing a report.
    """
    def mutate(cwd):
        # "fix" by gutting the existing test — the documented reward hack
        (cwd / "calc.py").write_text("def add(a, b):\n    return 0  # broken\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    pass\n"  # assertion removed
        )

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("make tests green", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_INPUT
    refreshed = await store.get_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_INPUT
    assert refreshed.blocker is not None
    # The card is written for a human who has never heard of the guard: the
    # headline says what changed in English, and the guard's own counters are
    # below the fold in the evidence, where a debugger wants them.
    assert "check less" in refreshed.blocker["question"]
    assert "->" not in refreshed.blocker["question"], "guard jargon in the headline"
    assert "TAMPERED" in refreshed.blocker["evidence"]
    # nothing was pushed as an approvable PR
    assert outcome.pr_url is None
    # The attempt row must SAY why it failed — tamper failures recorded only
    # test_results, leaving failure_reason empty (the "post-implement failure
    # reason came back EMPTY" observability gap, C2).
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "failed"
    assert (attempts[-1]["failure_reason"] or "").strip(), \
        "tamper-failed attempt has an empty failure_reason"


class _GutTestInOwnCommitBackend:
    """Guts the existing test inside its OWN git commit (the shape a resumed
    attempt's [WIP-BLOCKED] checkpoint has), then leaves only an innocent
    uncommitted change for the orchestrator to commit."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        (cwd / "calc.py").write_text("def add(a, b):\n    return 0  # broken\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    pass\n"
        )
        _git(cwd, "add", "-A")
        _git(cwd, "commit", "-m", "[WIP] make tests green")
        (cwd / "notes.md").write_text("did the work\n")
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Write",
                                tool_input={"file_path": "notes.md"}))
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_tamper_guard_sees_the_whole_branch_not_just_the_last_commit(
    bare_repo, tmp_path, store
):
    """ARCH_REVIEW B2 #3: the guard used to diff HEAD~1..HEAD while the
    reviewer and the PR ship merge-base..HEAD — so test-gutting buried in an
    earlier commit of the branch (a resumed attempt's checkpoint, or a commit
    the agent made itself) sailed through. The guard must inspect the same
    range that ships.

    (Terminal state is AWAITING_INPUT rather than ESCALATED since 2026-08-09 —
    see `test_tamper_weakening_is_blocked_and_stops_the_run`. What this test is
    about, the RANGE the guard inspects, is untouched by that.)"""
    cfg = _config(tmp_path)
    orch = Orchestrator(
        store, cfg.data, _GutTestInOwnCommitBackend(), SlackNotifier(None)
    )
    t = Task.new("make tests green", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert "TAMPERED" in (await store.get_task(t.id)).blocker["evidence"]
    assert outcome.pr_url is None


# --------------------------------------------------------------------------- #
# Phase 2: adversarial reviewer gate                                           #
# --------------------------------------------------------------------------- #

class FakeReviewer:
    """Injects a scripted ReviewDecision without running the LLM."""

    def __init__(self, decision: ReviewDecision, *, call_count: list | None = None):
        self._decision = decision
        self.calls: list[dict] = []
        self._call_count = call_count  # shared mutable list for multi-attempt tests

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                     before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        self.calls.append({"task_id": task.id, "mode": kwargs.get("mode"),
                           "claim_report": kwargs.get("claim_report")})
        if self._call_count is not None:
            self._call_count.append(1)
        return self._decision


async def test_reviewer_passes_proceeds_to_pr(bare_repo, tmp_path, store):
    """Correct change + passing reviewer → AWAITING_APPROVAL."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    passing_decision = ReviewDecision(
        passed=True,
        checklist=[
            ChecklistItem("mul(a,b) implemented", True, "calc.py:3 returns a*b"),
            ChecklistItem("tests added", True, "test_calc.py:5 test_mul asserts mul(2,3)==6"),
        ],
    )
    cfg = _config(tmp_path)
    reviewer = FakeReviewer(passing_decision)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    assert reviewer.calls  # reviewer was actually invoked
    # attempt records the review checklist
    attempts = await store.list_attempts(t.id)
    last = attempts[-1]
    assert last["review_passed"] == 1
    assert last["review_checklist"] is not None
    assert [e["kind"] for e in events].count("review") >= 1


async def test_the_draft_pr_url_REACHES_the_reviewer_end_to_end_0a(
        bare_repo, tmp_path, store, monkeypatch):
    """0a: the PR must exist BEFORE the gate runs, and its url must ARRIVE there.

    This is the seam two review rounds asked for. It is not a tautology: the url is
    handed to the FORGE stub (orch_mod.open_pr), never to reviewer.review. Every hop
    between them is the real thing — _open_draft_pr_for_review -> _run_review ->
    reviewer.review. Dropping `draft_pr=` at that last hop makes the entire 0a change
    inert (the gate is told no PR exists while one is open) and, before this test, the
    whole suite stayed green.

    The ordering assertion is 0a itself: open_pr is called, THEN the reviewer runs.
    On main the only open_pr call comes after the gate has already decided.

    Why github_hosts: a pre-gate draft is opened for GitHub remotes only (GitLab's
    open_mr is neither draft-by-default nor already-exists-idempotent). The bare
    fixture remote is a local path, so it is classified via the documented GHE
    `git.github_hosts` mechanism rather than by rewriting git plumbing —
    `remote.origin.pushurl` would NOT work, `git remote get-url` ignores it.
    """
    from no_human.core import orchestrator as orch_mod

    FORGE_URL = "https://github.com/o/r/pull/4242"
    order: list[str] = []
    opens: list[dict] = []

    def fake_open_pr(repo, branch, title, body, **kwargs):
        order.append("open_pr")
        opens.append({"body": body, "refresh": kwargs.get("update_existing_body")})
        return PrResult(url=FORGE_URL, kind="github", branch=branch)

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    seen_draft_pr: list = []

    class SpyReviewer(FakeReviewer):
        async def review(self, task, **kwargs):
            order.append("review")
            seen_draft_pr.append(kwargs.get("draft_pr"))
            return await super().review(task, **kwargs)

    cfg = _config(tmp_path)
    cfg.data.setdefault("git", {})["github_hosts"] = ["remote.git"]
    reviewer = SpyReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("mul(a,b) implemented", True, "calc.py:4 returns a*b"),
    ]))
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append, reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    await orch.run_task(t)

    assert reviewer.calls, "reviewer never ran — the test proves nothing"
    # 0a ordering: the artifact exists before the gate judges it.
    assert order[:2] == ["open_pr", "review"], order
    # 0a wiring: the url the forge returned is the url the gate was given.
    assert seen_draft_pr[0] == FORGE_URL, seen_draft_pr

    # CRITICAL-2, and the mutant that survived a whole extra round: opening the draft
    # early means `gh pr create` will find the PR already there at finalize time, so the
    # FINAL body — the one carrying the evidence sections — is written only if _finalize
    # asks for a refresh. Forcing update_existing_body=False there loses the evidence
    # permanently and every other test in the suite still passes. Assert the ORCHESTRATOR
    # sets it, not merely that github.open_pr honours it.
    assert len(opens) >= 2, f"expected a pre-gate open and a finalize open, got {opens}"
    assert opens[0]["refresh"] is not True, "the FIRST open creates the draft; nothing to refresh"
    assert opens[-1]["refresh"] is True, opens
    # and the refresh must actually carry the evidence the draft body could not have had
    # (the reviewer's verdict now leads the consolidated `## Evidence` area).
    assert "| Independent review |" in opens[-1]["body"], opens[-1]["body"][:400]


async def test_a_run_that_did_NOT_open_the_draft_never_rewrites_the_body_0a(
        bare_repo, tmp_path, store, monkeypatch):
    """HIGH-2 (review round 3, DRIVEN): `update_existing_body` meant "a url came back".

    The revision flow (`nh reject`, a PR comment) resumes onto a branch whose PR already
    exists. open_pr returned that url, the flag went True, and _finalize ran
    `gh pr edit --body <template>` over a description a HUMAN may have edited — behaviour
    main never had. vcs/github.py asserted in prose that "only the run that opened the
    draft may rewrite the body"; that sentence was false, and a review proved it by
    driving the pipeline.

    The right question is "did I CREATE this PR", which is now answered by asking the forge
    BEFORE opening, and persisted in task.context so a resume can still answer it.
    """
    from no_human.core import orchestrator as orch_mod
    from no_human.vcs import github as gh_mod

    URL = "https://github.com/o/r/pull/77"
    opens: list[dict] = []

    def fake_open_pr(repo, branch, title, body, **kwargs):
        opens.append({"refresh": kwargs.get("update_existing_body")})
        return PrResult(url=URL, kind="github", branch=branch)

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    # the PR is ALREADY open on this branch — this run is not its author
    monkeypatch.setattr(gh_mod, "_existing_pr_url", lambda repo_path, branch: URL)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    cfg.data.setdefault("git", {})["github_hosts"] = ["remote.git"]
    reviewer = FakeReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("mul(a,b) implemented", True, "calc.py:4 returns a*b"),
    ]))
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append, reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    await orch.run_task(t)

    assert opens, "open_pr never ran — the test proves nothing"
    assert not any(o["refresh"] for o in opens), (
        "REGRESSION: a run that did not create the PR asked the forge to REWRITE its "
        f"body — a human's edited description would be lost. calls={opens}")
    # and the durable claim must not have been staked either
    assert not (t.context or {}).get("pr_draft_created"), t.context


async def test_the_finalize_RETRY_still_refreshes_the_body_0a(
        bare_repo, tmp_path, store, monkeypatch):
    """HIGH-3 (review round 3, DRIVEN): the retry omitted `update_existing_body`.

    0a makes _finalize's `gh pr create` ALWAYS hit already-exists (the pre-gate draft is
    open), so if the first create fails transiently, the retry is the ONLY call that can
    write the final body. It was passing the kwarg on the first call and not the retry —
    measured flags [None, True, None] — so any task that hit one transient forge error
    kept the pre-review draft body permanently, with no evidence sections.
    """
    from no_human.core import orchestrator as orch_mod
    from no_human.vcs import github as gh_mod

    URL = "https://github.com/o/r/pull/78"
    opens: list[dict] = []
    calls = {"n": 0}

    def fake_open_pr(repo, branch, title, body, **kwargs):
        calls["n"] += 1
        opens.append({"n": calls["n"], "refresh": kwargs.get("update_existing_body"),
                      "body": body})
        if calls["n"] == 2:          # _finalize's first create fails transiently
            raise RuntimeError("gh pr create failed: unexpected EOF")
        return PrResult(url=URL, kind="github", branch=branch)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)
    monkeypatch.setattr(orch_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(gh_mod, "_existing_pr_url", lambda repo_path, branch: None)

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    cfg.data.setdefault("git", {})["github_hosts"] = ["remote.git"]
    reviewer = FakeReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("mul(a,b) implemented", True, "calc.py:4 returns a*b"),
    ]))
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=[].append, reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    await orch.run_task(t)

    assert len(opens) >= 3, f"expected draft + failed create + retry, got {opens}"
    retry = opens[-1]
    assert retry["refresh"] is True, (
        "REGRESSION: _finalize's transient retry dropped update_existing_body, so the PR "
        f"keeps the pre-review draft body forever. flags={[o['refresh'] for o in opens]}")
    assert "| Independent review |" in retry["body"], retry["body"][:300]


async def test_reviewer_fails_blocks_pr_and_loops(bare_repo, tmp_path, store):
    """Flawed change + failing reviewer → reviewer blocks; after max_attempts → ESCALATED."""
    call_count: list = []
    # Attempt 2 now branches from attempt 1's OWN rejected commit (the handoff
    # fix under test elsewhere), instead of re-branching from base. A fixed,
    # byte-identical rewrite would then be a genuine zero-diff against the
    # inherited tree — caught (correctly) as "agent produced no file changes"
    # rather than a second review-worthy attempt, which would make this test
    # escalate after only 1 reviewer call via the zero-diff path instead of
    # via the same-pass-rate stagnation path it exists to pin. A real coder
    # facing repeated review feedback would still touch something each turn,
    # so model that with a per-call marker — the review outcome stays fixed
    # (`failing_decision` below, unconditionally), only the diff needs to be
    # real.
    calls: list = []

    def mutate(cwd):
        calls.append(1)
        # Introduce a product file change without adequate tests
        (cwd / "calc.py").write_text(
            f"# attempt {len(calls)}\ndef add(a, b):\n    return 0  # broken impl\n")
        # No test changes — tamper guard stays clean, but reviewer catches the fault.

    failing_decision = ReviewDecision(
        passed=False,
        checklist=[
            ChecklistItem("add(a,b) returns correct sum", False,
                          "calc.py:2 returns 0, not a+b — implementation is wrong"),
            ChecklistItem("tests verify correctness", False,
                          "test_calc.py: existing test_add() would catch this; "
                          "tests were not updated to fail"),
        ],
    )
    cfg = _config(tmp_path)
    reviewer = FakeReviewer(failing_decision, call_count=call_count)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("fix add()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Escalated: stagnation detector fires after 2 identical failing attempts
    # (same review pass rate → agent is stuck), so only 2 reviewer calls.
    assert outcome.status is TaskStatus.ESCALATED
    assert outcome.pr_url is None
    assert len(call_count) == 2
    # Each attempt's review_passed is recorded as 0.
    attempts = await store.list_attempts(t.id)
    assert all(a["review_passed"] == 0 for a in attempts)


class SequencedFakeReviewer:
    """Returns a different scripted ReviewDecision per call, in order (the
    last one repeats if more calls arrive than decisions) — for testing
    multi-attempt review-driven retry behavior where each attempt's
    findings genuinely differ."""

    def __init__(self, decisions: list):
        self._decisions = decisions
        self.calls: list = []

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                     before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        self.calls.append({"task_id": task.id})
        idx = min(len(self.calls) - 1, len(self._decisions) - 1)
        return self._decisions[idx]


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_stagnation_not_triggered_by_different_findings_same_rate(bare_repo, tmp_path, store):
    """D6 regression: a matching 0% pass rate across 2 attempts must NOT be
    treated as stagnation when the specific failing findings are entirely
    different each time — that is real incremental progress (previous
    issues fixed, new ones surfaced), not the agent stuck repeating the
    same mistake. Modeled on a real run that hit exactly this pattern.

    Each attempt now branches from the PREVIOUS attempt's own gate-failed
    commit (this task's fix) instead of re-branching from base. A fixed,
    byte-identical rewrite on attempts 2/3 would be a genuine zero-diff
    against the inherited tree — caught by the honesty gate as "agent
    produced no file changes" rather than reaching review again, which
    would make this test escalate via the zero-diff path after only 1 real
    reviewer call instead of exercising the same-pass-rate/different-
    findings D6 logic it exists to pin. The code is already correct on
    attempt 1; what's still "wrong" is unrelated (the reviewer's scripted
    Jenkinsfile findings), so a realistic coder would still touch something
    each turn in response — model that with a per-call counter so every
    attempt is a real (if still review-failing, until the last) diff.
    """
    calls: list = []

    def mutate(cwd):
        calls.append(1)
        (cwd / "calc.py").write_text(
            f"# attempt {len(calls)}\n"
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    decisions = [
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("commitSha undefined reference", False, "Jenkinsfile:883"),
            ChecklistItem("PR comment pagination missing per_page param", False, "Jenkinsfile:760"),
        ]),
        ReviewDecision(passed=False, checklist=[
            ChecklistItem("Image reuse broken across Jenkins agents", False, "Jenkinsfile:653"),
            ChecklistItem("Selfcheck fixture passes for the wrong reason", False, "scripts/x.groovy:52"),
        ]),
        ReviewDecision(passed=True, checklist=[
            ChecklistItem("all findings addressed", True, "verified"),
        ]),
    ]
    cfg = _config(tmp_path)
    reviewer = SequencedFakeReviewer(decisions)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("fix things", repo_path=str(bare_repo))
    t.acceptance_criteria = ["things are fixed"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Must reach and succeed on attempt 3 — NOT escalate after attempt 2 on
    # a false stagnation positive (both attempts scored 0%, but zero
    # findings recurred between them).
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is not None
    assert len(reviewer.calls) == 3


async def test_implement_prompt_uses_worktree_dir_not_primary_checkout(bare_repo, tmp_path, store):
    """Regression (validation found this): in concurrency mode the agent runs in a
    per-task worktree, so the prompt must point at that working dir — NOT
    task.repo_path (the primary checkout). Handing it the primary path made the
    agent edit the wrong tree; the worktree then showed 'no file changes'."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add f()", repo_path="/primary/checkout")
    t.acceptance_criteria = ["f exists"]

    wt = "/tmp/worktrees/abc123"
    prompt = orch._build_implement_prompt(t, wt)
    assert wt in prompt
    assert "make ALL edits here" in prompt
    # The primary checkout must NOT be presented as the working directory.
    assert "repo at /primary/checkout" not in prompt

    # Backward-compat: with no work_dir it falls back to task.repo_path.
    assert "/primary/checkout" in orch._build_implement_prompt(t)


async def test_review_feedback_injected_into_next_attempt(bare_repo, tmp_path, store):
    """EVOLUTION_PLAN §2.2: on reviewer FAIL the cited findings are persisted and
    surface in the next attempt's implement prompt (no new loop — reuses the
    bounded attempt machinery; tamper guard still gates each round)."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("fix add()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(t)

    failed = [
        ChecklistItem("add(a,b) returns correct sum", False,
                      "calc.py:2 returns 0, not a+b", file="calc.py", line=2,
                      comment="Return a + b, not a hardcoded 0."),
    ]
    await orch._record_review_feedback(t, failed)

    refreshed = await store.get_task(t.id)
    assert refreshed.context["review_feedback"][0]["file"] == "calc.py"

    prompt = orch._build_implement_prompt(refreshed)
    assert "independent staff reviewer FAILED" in prompt
    assert "Return a + b, not a hardcoded 0." in prompt
    assert "calc.py:2" in prompt
    # The anti-tamper instruction must ride along with the feedback.
    assert "do NOT weaken" in prompt.lower() or "do not weaken" in prompt.lower()


async def test_red_team_agent_weakens_test_tamper_guard_blocks(bare_repo, tmp_path, store):
    """Red-team: agent guts the test to make a broken impl pass.

    The tamper guard fires BEFORE the REVIEW GATE runs, so a tampered diff is
    never put in front of the gate at all — that is the invariant here and it
    is unchanged.

    Since 2026-08-09 the fire does buy exactly one reviewer session: the tamper
    ADJUDICATION, which is a different question ("does the ticket require these
    test changes?") on a different, minimal input. This test pins the
    difference — every reviewer call on this path must be an adjudication, and
    none may be a gate review. The scripted decision carries no adjudication
    verdict, which is CANNOT_DECIDE, which parks.
    """
    call_count: list = []

    def mutate(cwd):
        # Agent "fixes" broken impl by removing the assertion that catches it.
        (cwd / "calc.py").write_text("def add(a, b):\n    return 0  # still broken\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    pass  # assertion deleted\n"
        )

    passing_decision = ReviewDecision(passed=True, checklist=[
        ChecklistItem("all good", True, "looks fine"),
    ])
    cfg = _config(tmp_path)
    reviewer = FakeReviewer(passing_decision, call_count=call_count)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("make tests pass", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Tamper guard fires; the REVIEW GATE never sees the tampered diff.
    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert "TAMPERED" in (await store.get_task(t.id)).blocker["evidence"]
    assert reviewer.calls, "the fire must be adjudicated, not filed at a human"
    assert all(c["mode"] == "tamper_adjudication" for c in reviewer.calls), (
        "a tampered diff reached the review gate; the guard is pre-review and "
        f"must stay there. calls: {[c['mode'] for c in reviewer.calls]}")
    assert len(call_count) == len(reviewer.calls)


# --------------------------------------------------------------------------- #
# Phase 5: agent-emitted structured blockers (Part 22)                        #
# --------------------------------------------------------------------------- #

class BlockerBackend:
    """A backend that emits a structured BLOCKER_JSON block instead of finishing.

    Models the agent hitting something it cannot solve without lowering the bar.
    Optionally mutates files first (to test that WIP is checkpointed).
    """

    def __init__(self, blocker_json: str, *, mutate=None):
        self._json = blocker_json
        self._mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        if self._mutate:
            self._mutate(cwd)
        text = (
            "I cannot proceed without lowering the bar.\n"
            "BLOCKER_JSON_START\n" + self._json + "\nBLOCKER_JSON_END\n"
        )
        return AgentResult(final_text=text, num_turns=1, is_error=False,
                           tokens_used=50, session_id="s", stop_reason="end_turn")


async def test_agent_ambiguity_blocker_routes_to_awaiting_input(bare_repo, tmp_path, store):
    """An AMBIGUITY blocker parks the task in awaiting_input with its question —
    never guesses, never fakes done (22.2)."""
    bjson = (
        '{"category": "AMBIGUITY", "confidence": 0.9, '
        '"root_cause_hypothesis": "criterion 2 contradicts criterion 1", '
        '"question": "Which behavior is correct for empty input?", '
        '"options": ["raise", "return 0"], '
        '"goal": "implement parse()", "evidence": "$ grep ...\\nno spec found"}'
    )
    cfg = _config(tmp_path)
    notes = []
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson), SlackNotifier(None),
                        event_sink=notes.append)
    t = Task.new("parse input", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_INPUT
    assert outcome.pr_url is None
    refreshed = await store.get_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_INPUT
    assert refreshed.blocker["category"] == "AMBIGUITY"
    assert refreshed.blocker["question"]
    assert "awaiting_input" in [e["kind"] for e in notes]


async def test_agent_impossible_blocker_escalates_not_faked(bare_repo, tmp_path, store):
    """The DoD red-team case: a deliberately-impossible task is escalated with
    evidence, never faked done."""
    bjson = (
        '{"category": "IMPOSSIBLE", "confidence": 0.95, '
        '"root_cause_hypothesis": "requested API does not exist in this version", '
        '"question": "This cannot be done as specified; drop or change it?", '
        '"goal": "call nonexistent API", "evidence": "ImportError: no such symbol"}'
    )
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson), SlackNotifier(None))
    t = Task.new("impossible task", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    assert outcome.pr_url is None
    refreshed = await store.get_task(t.id)
    assert refreshed.blocker["category"] == "IMPOSSIBLE"


async def test_agent_dependency_wait_parks_and_checkpoints_wip(bare_repo, tmp_path, store):
    """A high-confidence DEPENDENCY_WAIT parks (blocked) with a wake condition and
    the partial work is checkpointed as [WIP-BLOCKED]."""
    def mutate(cwd):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b  # WIP\n")

    bjson = (
        '{"category": "DEPENDENCY_WAIT", "confidence": 0.9, '
        '"wake_condition": "pr_merged:org/repo#42", '
        '"root_cause_hypothesis": "needs upstream PR #42 merged first", '
        '"goal": "use new upstream helper", "evidence": "import fails until #42 lands"}'
    )
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson, mutate=mutate),
                        SlackNotifier(None))
    t = Task.new("use upstream helper", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.BLOCKED
    refreshed = await store.get_task(t.id)
    assert refreshed.blocker["wake_condition"] == "pr_merged:org/repo#42"
    assert refreshed.wake_check_at is not None  # watcher will re-evaluate
    # WIP was committed as [WIP-BLOCKED] on the feature branch.
    log = subprocess.run(["git", "log", "--all", "--oneline"], cwd=bare_repo,
                         capture_output=True, text=True).stdout
    assert "WIP-BLOCKED" in log or refreshed.blocker["resume_commit"]


async def test_low_confidence_dependency_wait_escalates(bare_repo, tmp_path, store):
    """Unsure-what's-wrong (confidence < threshold) escalates instead of parking
    silently (Part 22 config: escalate_on_low_confidence_below)."""
    bjson = (
        '{"category": "DEPENDENCY_WAIT", "confidence": 0.3, '
        '"wake_condition": "after:2h", '
        '"root_cause_hypothesis": "maybe a dependency? not sure", '
        '"question": "Unclear why this fails — advise?", '
        '"goal": "build", "evidence": "intermittent failure"}'
    )
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson), SlackNotifier(None))
    t = Task.new("flaky build", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED


class PromptCapturingBackend:
    """First run emits an AMBIGUITY blocker; second run (after the human reply)
    records the prompt it received and applies a real fix."""

    def __init__(self, blocker_json, fix):
        self._json = blocker_json
        self._fix = fix
        self.calls = 0
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None, on_event=None,
                  supervisor_hook=None, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            text = "Need a decision.\nBLOCKER_JSON_START\n" + self._json + "\nBLOCKER_JSON_END\n"
            return AgentResult(final_text=text, num_turns=1, is_error=False,
                               tokens_used=30, session_id="s", stop_reason="end_turn")
        self._fix(cwd)
        return AgentResult(final_text="applied the agreed behavior", num_turns=2,
                           is_error=False, tokens_used=80, session_id="s",
                           stop_reason="end_turn")


async def test_reply_resumes_from_checkpoint_with_human_answer(bare_repo, tmp_path, store):
    """DoD: a parked task resumes from its checkpoint when a human replies, and
    the resumed (fresh) session is seeded with the human's answer."""
    bjson = (
        '{"category": "AMBIGUITY", "confidence": 0.9, '
        '"root_cause_hypothesis": "empty-input behavior unspecified", '
        '"question": "What should mul() do on empty input?", '
        '"options": ["raise", "return 0"], "goal": "implement mul", '
        '"evidence": "spec silent on empty input"}'
    )

    def fix(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    backend = PromptCapturingBackend(bjson, fix)
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    # 1. First run parks in awaiting_input with the question.
    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_INPUT
    # base branch was captured as main and persisted (not the feature branch).
    parked = await store.get_task(t.id)
    assert parked.context["base_branch"] == "main"

    # 2. Simulate `nh reply <id> "return 0"`: store the answer, resume.
    refreshed = await store.get_task(t.id)
    ctx = refreshed.context or {}
    ctx["human_replies"] = [{"at": "2026-06-22", "question": "empty input?",
                             "answer": "return 0 on empty input"}]
    refreshed.context = ctx
    refreshed.wake_check_at = None
    await store.update_task(refreshed)
    await store.set_status(refreshed, TaskStatus.IMPLEMENTING, validate=False)

    # 3. Re-run: resumes from the checkpoint and completes to a PR.
    outcome2 = await orch.run_task(refreshed)
    assert outcome2.status is TaskStatus.AWAITING_APPROVAL
    assert outcome2.pr_url is not None

    # The resumed (fresh) session prompt carried the human's answer (22.5).
    resume_prompt = backend.prompts[-1]
    assert "return 0 on empty input" in resume_prompt
    assert "do NOT re-ask" in resume_prompt
    # Resume must re-base from main, not the parked feature branch.
    final = await store.get_task(t.id)
    assert final.context["base_branch"] == "main"


async def test_resume_after_wip_checkpoint_rebases_from_main(bare_repo, tmp_path, store):
    """A DEPENDENCY_WAIT parks with a [WIP-BLOCKED] commit on a feature branch.
    On resume the base must still be main — not the feature branch (which would
    make open_pr use base == head)."""
    bjson = (
        '{"category": "DEPENDENCY_WAIT", "confidence": 0.9, '
        '"wake_condition": "pr_merged:org/repo#42", '
        '"root_cause_hypothesis": "needs upstream PR", "goal": "use helper", '
        '"evidence": "import fails"}'
    )

    def wip(cwd):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b  # partial WIP\n")

    def fix(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    backend = PromptCapturingBackend(bjson, fix)
    # First call mutates WIP then parks; override call 1 to also write WIP.
    backend._fix = fix  # used on call 2

    class _WipFirst:
        def __init__(self, inner, wip):
            self.inner = inner
            self.wip = wip
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None, on_event=None,
                      supervisor_hook=None, **kwargs):
            if self.inner.calls == 0:
                self.wip(cwd)
            return await self.inner.run(prompt, cwd=cwd, max_turns=max_turns,
                                        effort=effort, resume=resume, on_event=on_event,
                                        supervisor_hook=supervisor_hook)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, _WipFirst(backend, wip), SlackNotifier(None))
    t = Task.new("use helper", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul works"]
    await store.create_task(t)

    o1 = await orch.run_task(t)
    assert o1.status is TaskStatus.BLOCKED
    parked = await store.get_task(t.id)
    assert parked.context["base_branch"] == "main"
    # WIP was checkpointed.
    log = subprocess.run(["git", "log", "--all", "--oneline"], cwd=bare_repo,
                         capture_output=True, text=True).stdout
    assert "WIP-BLOCKED" in log

    # Resume (simulate nh unblock → implementing) and complete.
    await store.set_status(parked, TaskStatus.IMPLEMENTING, validate=False)
    o2 = await orch.run_task(parked)
    assert o2.status is TaskStatus.AWAITING_APPROVAL
    final = await store.get_task(t.id)
    assert final.context["base_branch"] == "main"


# --------------------------------------------------------------------------- #
# Regression: agent hitting max_turns must escalate via the bounded loop,      #
# never crash the orchestrator (shadow-validation finding, 2026-06-22).        #
# --------------------------------------------------------------------------- #

class MaxTurnsBackend:
    """Backend that always returns a terminal max_turns error (as the real
    ClaudeBackend now does when the SDK raises 'maximum number of turns')."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        if on_event:
            on_event(AgentEvent("result", text="Reached maximum number of turns (40)"))
        return AgentResult(
            final_text="Reached maximum number of turns (40)",
            num_turns=max_turns, is_error=True, tokens_used=1234,
            session_id="s", stop_reason="max_turns",
        )


async def test_agent_max_turns_escalates_not_crashes(bare_repo, tmp_path, store):
    cfg = _config(tmp_path)
    backend = MaxTurnsBackend()
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("do the hard thing", repo_path=str(bare_repo))
    await store.create_task(t)

    # Must NOT raise — the whole point of the fix.
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    # The bounded loop ran every attempt, then escalated honestly.
    # W4 (failed-restoration fingerprint): every attempt hits max_turns with
    # the same empty diff — the second identical outcome stops the loop.
    assert backend.calls == 2
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 2
    assert all(a["status"] == "failed" for a in attempts)
    assert all("max_turns" in (a.get("failure_reason") or "") for a in attempts)
    # No half-finished work was committed/pushed as an approvable PR. (A local
    # attempt branch may exist — it's created before the agent runs — but the
    # remote received no pushed branch.)
    assert outcome.pr_url is None
    remote_branches = subprocess.run(
        ["git", "ls-remote", "--heads", "origin"], cwd=bare_repo,
        capture_output=True, text=True).stdout
    assert "no-human/" not in remote_branches
    assert "agent_error" in [e["kind"] for e in events]


# --------------------------------------------------------------------------- #
# B5 regression: revision must reuse the existing PR branch                     #
# --------------------------------------------------------------------------- #

async def test_revision_reuses_pr_branch_b5(bare_repo, tmp_path, store):
    """B5: a revision (PR comment / nh reject) must push to the SAME branch the
    PR was opened on.  Before the fix the attempt loop restarted at attempt_n=1,
    computed a DIFFERENT branch name, and opened a duplicate PR."""
    call_count = 0

    def mutate(cwd):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\n"
                "def mul(a, b):\n    return a * b\n"
            )
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n"
            )
        else:
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    \"\"\"Add.\"\"\"\n    return a + b\n\n"
                "def mul(a, b):\n    \"\"\"Multiply.\"\"\"\n    return a * b\n"
            )
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n"
            )

    cfg = _config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    # --- Run 1: original work → PR opened ---
    outcome1 = await orch.run_task(t)
    assert outcome1.status is TaskStatus.AWAITING_APPROVAL
    t = await store.get_task(t.id)
    assert t.context.get("pr_branch"), "pr_branch must be stored on PR open"
    original_branch = t.context["pr_branch"]

    # --- Simulate PR comment → task resumed for revision ---
    ctx = t.context
    ctx["send_back_feedback"] = [{"at": "2026-06-28T12:00:00Z", "message": "add docstrings"}]
    t.context = ctx
    await store.update_task(t)
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)

    # --- Run 2: revision → must reuse the same branch ---
    events.clear()
    outcome2 = await orch.run_task(t)
    assert outcome2.status is TaskStatus.AWAITING_APPROVAL

    t = await store.get_task(t.id)
    assert t.context["pr_branch"] == original_branch, (
        f"revision changed pr_branch from {original_branch!r} to "
        f"{t.context['pr_branch']!r} — B5 bug: duplicate PR"
    )
    # The attempt record must also show the original branch.
    attempts = await store.list_attempts(t.id)
    revision_attempt = attempts[-1]
    assert revision_attempt["branch_name"] == original_branch


# --------------------------------------------------------------------------- #
# Phase 1: plan-first worker                                                   #
#                                                                               #
# All tests mock ClaudeBackend — no real Claude API calls.  _config() disables  #
# planning by default; tests that exercise planning re-enable it explicitly.    #
# --------------------------------------------------------------------------- #

from unittest.mock import patch as _patch
from no_human.vcs import GitRepo


def _planning_config(tmp_path):
    """Config with planning explicitly enabled (for planning-specific tests)."""
    cfg = _config(tmp_path)
    cfg.data["planning"]["enabled"] = True
    return cfg


class PlannerBackend:
    """A backend that returns a scripted plan text (no real LLM)."""

    def __init__(self, plan_text: str):
        self._plan = plan_text

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(final_text=self._plan, num_turns=3, is_error=False,
                           tokens_used=200, session_id="s", stop_reason="end_turn")


class FailingPlannerBackend:
    """A backend that always raises — simulates SDK auth failure."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        raise RuntimeError("no auth token")


_SAMPLE_PLAN = (
    "## FILES TO CHANGE/CREATE\n- calc.py: add mul()\n\n"
    "## APPROACH\nAdd a mul function.\n\n"
    "## TEST PLAN\ntest_mul asserts mul(2,3)==6.\n\n"
    "## OUT OF SCOPE\nDo not rename existing functions.\n\n"
    "## VERIFICATION\npytest -q\n"
)


async def test_planning_generates_and_stores_plan(bare_repo, tmp_path, store):
    """_generate_plan stores the plan text in task.context['plan']."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(_SAMPLE_PLAN)):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == _SAMPLE_PLAN.strip()
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("plan generated" in e.get("text", "") for e in planning_events)


class PromptCapturingPlannerBackend:
    """Captures every prompt sent to this backend so tests can assert on
    content. A list, not a single attribute: MoA planning (on by default)
    fans out multiple calls (one per proposer + one aggregator) through the
    same patched backend, so the last call alone isn't representative."""

    def __init__(self, plan_text: str):
        self._plan = plan_text
        self.prompts: list[str] = []

    @property
    def prompt(self) -> str | None:
        """Back-compat: the most recent prompt, for single-call call sites."""
        return self.prompts[-1] if self.prompts else None

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.prompts.append(prompt)
        return AgentResult(final_text=self._plan, num_turns=3, is_error=False,
                           tokens_used=200, session_id="s", stop_reason="end_turn")


async def test_planner_prompt_no_child_tasks_by_default(bare_repo, tmp_path, store):
    """By default the planner is told to delegate in-session, NOT to emit a
    DECOMPOSE_PLAN (which would create child tasks). Checked across every MoA
    proposer prompt — each carries the full base prompt plus its own lens."""
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("multi-concern task", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert backend.prompts
    assert all("DECOMPOSE_PLAN_START" not in p for p in backend.prompts)
    assert any("IN-SESSION DELEGATION" in p for p in backend.prompts)
    assert any("must never create new tasks" in p for p in backend.prompts)


def _base_prompts(backend):
    """Prompts derived from the planner base prompt — every MoA proposer's, and
    the single planner's. The MoA aggregator's prompt carries only the drafts."""
    return [p for p in backend.prompts
            if "You are planning an implementation task" in p]


async def test_planner_prompt_carries_linked_repos(bare_repo, tmp_path, store):
    """D19: the planner runs with cwd=primary repo and was never told the linked
    repos exist, so it planned around them as if they were not on disk. Every
    proposer must get the path map."""
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("multi-repo task", repo_path=str(bare_repo))
    t.linked_repos = ["/repos/metrics-core-service"]
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    prompts = _base_prompts(backend)
    assert prompts
    assert all("/repos/metrics-core-service" in p for p in prompts)
    assert all("Never assume a linked repo is absent" in p for p in prompts)


async def test_planner_prompt_carries_repo_conventions(bare_repo, tmp_path, store):
    """The planner receives the target repo's own conventions EXPLICITLY.

    It used to receive them by accident: read-only sessions emitted no
    `--setting-sources` flag, so the SDK loaded the repo's instruction files on
    its own. Closing that leak (the repo under review was instructing the
    reviewer judging it) took the planner's conventions with it, because one
    mechanism served both. This pins the deliberate replacement, so the next
    person to touch `setting_sources` cannot silently un-inject it.

    This config does NOT fan out (the MoA gate needs signals), so this test
    sees exactly one prompt and says so with an assertion rather than an
    `all()` over a single element. The MoA path has its own test below — an
    earlier version of this docstring claimed to cover it, and a planted mutant
    that stripped the conventions from the proposer path alone survived the
    whole file.
    """
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    (bare_repo / "AGENTS.md").write_text("PLANNER_CONVENTION_MARKER: use tabs.")
    t = Task.new("a task", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    prompts = _base_prompts(backend)
    assert len(prompts) == 1, (
        f"expected the single-planner path, captured {len(prompts)} base "
        "prompts — if MoA now fires by default this test's scope claim is wrong")
    assert "PLANNER_CONVENTION_MARKER: use tabs." in prompts[0]
    # ADVISORY, never the coder's "AUTHORITATIVE … follow these over generic
    # guidance". That header would rank repo-authored text above the planner's
    # own directives, and the plan feeds `declared_files`, which the coder's
    # scope guard reads. The framing is the guard here, so pin it.
    assert "advisory" in prompts[0].lower()
    assert "AUTHORITATIVE" not in prompts[0], (
        "the planner must not be told the repo's files outrank its instructions")
    # HONEST SCOPE, because the assertion above reads stronger than it is: this
    # fixture writes a benign AGENTS.md, so it pins the header WE emit and
    # nothing more. Repo text is interpolated verbatim, so a repo that writes
    # the word AUTHORITATIVE — or "ignore the label above" — puts it in the
    # prompt and no assertion here can stop it. An independent reviewer
    # demonstrated exactly that. The framing is a real improvement over handing
    # the planner the coder's authority header; it is NOT a containment
    # boundary, and this test must not be read as claiming one.
    # And the subordination clause must name what is genuinely below it in THIS
    # prompt. The coder's clause says "the standing safety rules below still
    # apply", which is true there and vacuous here — no safety rules follow.
    assert "planning instructions below" in prompts[0]


async def test_planner_prompt_unchanged_when_repo_has_no_conventions(
    bare_repo, tmp_path, store
):
    """A repo that declares no conventions gets NO conventions block — not an
    empty header, not a stray blank. The guard against 'fixed one path,
    perturbed every other repo'.

    Scoped honestly: this asserts the block is ABSENT, which is what it can
    see. It does not assert byte-identity against the pre-change prompt — that
    needs the old revision to compare against, and a docstring claiming it
    while asserting two `not in`s is the kind of overclaim this file has been
    bitten by."""
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    # A PRECONDITION, asserted, not a cleanup loop. `bare_repo` is
    # function-scoped, so it never carries an instruction file and the deletion
    # this used to do could not fire — a no-op dressed as a safeguard, which
    # reads as protection and provides none. Assert the state instead, so the
    # day `bare_repo` starts seeding one, this fails loudly rather than quietly
    # testing nothing. Derived from the orchestrator's own list so a third
    # recognised filename is covered with no edit here.
    present = [r for r in orch._REPO_INSTRUCTION_FILES if (bare_repo / r).is_file()]
    assert not present, (
        f"bare_repo now seeds {present}; this test needs a repo that declares "
        "no conventions, so it is no longer testing what it claims")
    t = Task.new("a task", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    prompts = _base_prompts(backend)
    assert prompts
    assert all("advisory" not in p.lower() for p in prompts)
    assert all("--- AGENTS.md ---" not in p for p in prompts)


async def test_every_moa_proposer_gets_the_repo_conventions(bare_repo, tmp_path, store):
    """The conventions must reach EVERY proposer, not just the lone planner.

    This test exists because its absence was proven, not suspected. An
    independent reviewer planted a mutant that stripped the conventions from
    `_generate_plan_moa`'s `base_prompt` only, left the single-planner path
    intact, and ran the whole file: 149 passed. A regression down the fan-out
    path was invisible.

    `min_signals=0` forces the fan-out, so this exercises the proposer path
    rather than the complexity gate.
    """
    cfg = _moa_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    (bare_repo / "AGENTS.md").write_text("MOA_CONVENTION_MARKER: prefer composition.")
    t = Task.new("a task", repo_path=str(bare_repo))
    await store.create_task(t)
    fake = MoAFakeBackend(
        proposals={
            "minimal-first": _SAMPLE_PLAN,
            "risk-first": _SAMPLE_PLAN,
            "test-first": _SAMPLE_PLAN,
        },
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    prompts = _base_prompts(fake)
    # The count is the load-bearing part: `all()` over one element is what let
    # the mutant survive last time.
    assert len(prompts) == 3, (
        f"expected 3 proposer prompts, captured {len(prompts)} — the fan-out "
        "did not fire, so this asserts nothing about the MoA path")
    assert all("MOA_CONVENTION_MARKER: prefer composition." in p for p in prompts)
    assert all("AUTHORITATIVE" not in p for p in prompts)


async def test_planner_prompt_unchanged_for_single_repo(bare_repo, tmp_path, store):
    """No linked repos → no block, so the cacheable prefix is untouched."""
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("single-repo task", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert backend.prompts
    assert all("LINKED REPOSITORIES" not in p for p in backend.prompts)


async def test_planning_skips_for_code_review(bare_repo, tmp_path, store):
    """Planning is gated: code_review kind skips it entirely (no Claude call)."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("review PR #42", repo_path=str(bare_repo), kind="code_review")

    # No mock needed — code_review returns before creating a backend.
    result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == ""
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("code_review" in e.get("text", "") for e in planning_events)


async def test_planning_skips_when_disabled(bare_repo, tmp_path, store):
    """Planning respects the config gate (no Claude call)."""
    cfg = _config(tmp_path)  # planning already disabled by _config()
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))

    # No mock needed — disabled returns before creating a backend.
    result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == ""
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("disabled" in e.get("text", "") for e in planning_events)


async def test_planning_skip_plan_response(bare_repo, tmp_path, store):
    """When the planner assesses a trivial task, SKIP_PLAN bypasses planning."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("fix typo", repo_path=str(bare_repo))

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend("SKIP_PLAN")):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == ""
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("trivial" in e.get("text", "") for e in planning_events)


async def test_planning_failure_is_best_effort(bare_repo, tmp_path, store):
    """Planning failure doesn't crash — returns empty string (mocked failure)."""
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=FailingPlannerBackend()):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == ""
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("failed" in e.get("text", "") for e in planning_events)


# --------------------------------------------------------------------------- #
# The planner's ERRORED result must never become the plan.                     #
#                                                                              #
# A terminal SDK failure is not an exception the planner's try/except sees: the #
# backend swallows it and YIELDS a result event carrying the error string as   #
# its text, with is_error=True (claude_backend.py, the `except Exception` tail  #
# of stream()). The coder path has always gated on `result.is_error`; the      #
# planner path did not, so that one sentence was persisted as task.context      #
# ['plan'], written to .no_human/PLAN.md and inlined into the coder prompt      #
# under "IMPLEMENTATION PLAN (follow this plan closely...)" — while             #
# TaskSpec.from_plan parsed it to an EMPTY spec, silently emptying the scope    #
# guard, the test-plan block and the supervisor's file list.                   #
# --------------------------------------------------------------------------- #

#: Verbatim shape of the SDK's max-turns failure text, as `stream()` puts it on
#: the corrective result event (`msg` is kept clean for max_turns — no traceback
#: is appended), and as `run()` then hands it back as `final_text`.
_MAX_TURNS_ERROR = (
    "Claude Code returned an error result: Reached maximum number of turns (10)"
)


class ErroringPlannerBackend:
    """A backend whose run() RETURNS (never raises) a terminal-error result.

    This is the shape that actually reaches `_generate_plan` in production —
    `FailingPlannerBackend` above (which raises) exercises a different, already
    guarded path.
    """

    def __init__(self, text: str = _MAX_TURNS_ERROR,
                 stop_reason: str = "max_turns"):
        self._text = text
        self._stop = stop_reason

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(final_text=self._text, num_turns=10, is_error=True,
                           tokens_used=1234, session_id="s",
                           stop_reason=self._stop)


def _single_planner_config(tmp_path):
    """Planning on, MoA fan-out off — pins these tests to the single-planner
    path regardless of what the complexity gate would decide."""
    cfg = _planning_config(tmp_path)
    cfg.data.setdefault("llm", {})["moa_planning"] = {"enabled": False}
    return cfg


async def test_planner_error_result_never_becomes_the_plan(
    bare_repo, tmp_path, store,
):
    """The 74-character SDK error string must not be persisted, materialized
    or inlined into the coder prompt as an implementation plan."""
    cfg = _single_planner_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=ErroringPlannerBackend()):
        plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert plan == ""
    # …so the whole propagation surface stays empty. `_persist_plan` no-ops on a
    # falsy plan, which is also the guard on the .no_human/PLAN.md write.
    await orch._persist_plan(t, plan)
    assert not (t.context or {}).get("plan")
    prompt = orch._build_implement_prompt(t)
    # No plan is inlined as an implementation contract...
    assert "IMPLEMENTATION PLAN (follow this plan closely" not in prompt
    assert "the full plan is at `.no_human/PLAN.md`" not in prompt
    assert _MAX_TURNS_ERROR not in prompt
    # ...and the coder is told the block is missing because planning FAILED (R3),
    # not because the task was trivial enough to need no plan.
    assert "NO IMPLEMENTATION PLAN EXISTS" in prompt

    planning = [e for e in events if e.get("kind") == "planning"]
    # The failure is named out loud: what stopped it, how far it got, what it
    # cost, and what it actually said.
    failed = [e for e in planning if "max_turns" in e.get("text", "")]
    assert failed, planning
    assert "10 turns" in failed[0]["text"]
    assert "1234 tokens" in failed[0]["text"]
    assert "Reached maximum number of turns" in failed[0]["text"]


class ScriptedPlannerBackend:
    """Returns a scripted `AgentResult` per call and records the turn budget
    each call was given — the seam R3's retry has to be asserted at."""

    def __init__(self, *results):
        self._results = list(results)
        self.budgets: list[int] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.budgets.append(max_turns)
        return self._results[min(len(self.budgets) - 1, len(self._results) - 1)]


def _exhausted(turns: int) -> AgentResult:
    return AgentResult(
        final_text="Claude Code returned an error result: Reached maximum "
                   f"number of turns ({turns})",
        num_turns=turns, is_error=True, tokens_used=1234, session_id="s",
        stop_reason="max_turns")


def _r3_config(tmp_path):
    """Single-planner path at the LIVE planning.max_turns (24), which is what
    the 14 August tasks actually ran under (code default is 10)."""
    cfg = _single_planner_config(tmp_path)
    cfg.data["planning"]["max_turns"] = 24
    return cfg


async def test_planner_turn_exhaustion_retries_once_at_double_turns(
    bare_repo, tmp_path, store,
):
    """R3: a planner that burns its turn cap had its whole spend discarded and
    the coder ran plan-less. Retry ONCE at double the budget instead."""
    cfg = _r3_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    backend = ScriptedPlannerBackend(
        _exhausted(24),
        AgentResult(final_text=_SAMPLE_PLAN, num_turns=30, is_error=False,
                    tokens_used=900, session_id="s", stop_reason="end_turn"),
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert backend.budgets == [24, 48], backend.budgets
    assert plan == _SAMPLE_PLAN.strip()
    assert not (t.context or {}).get("plan_unavailable")
    texts = [e.get("text", "") for e in events if e.get("kind") == "planning"]
    assert any("retrying once at 48" in x for x in texts), texts


async def test_planner_exhausted_twice_tells_the_coder_there_is_no_plan(
    bare_repo, tmp_path, store,
):
    """R3: bounded at exactly one retry — and when it also exhausts, the coder
    is TOLD there is no plan (an informed coder beats a deceived one)."""
    cfg = _r3_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    backend = ScriptedPlannerBackend(_exhausted(24), _exhausted(48))
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert plan == ""
    assert backend.budgets == [24, 48], "exactly one retry, no new loop"
    prompt = orch._build_implement_prompt(t)
    assert "NO IMPLEMENTATION PLAN" in prompt
    assert "ran out of turns twice (24 then 48)" in prompt
    # The error string itself still never reaches the coder (1bb3be36 holds).
    assert "Reached maximum number of turns" not in prompt
    texts = [e.get("text", "") for e in events if e.get("kind") == "planning"]
    assert any("no plan for the coder" in x and "out of turns twice" in x
               for x in texts), texts


async def test_a_later_planning_round_clears_the_stale_no_plan_notice(
    bare_repo, tmp_path, store,
):
    """The notice describes THIS round's planning. A first round that died and a
    second that legitimately skipped (`SKIP_PLAN` — trivial one-line diff) must
    not leave the coder reading "planning FAILURE" about a planner that just
    called the task trivial. `_replan_for_approval` re-enters here, so a marker
    that is only ever set is a lie by the second round."""
    cfg = _r3_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=[].append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=ScriptedPlannerBackend(_exhausted(24), _exhausted(48))):
        assert await orch._generate_plan(t, GitRepo(bare_repo)) == ""
    assert (t.context or {}).get("plan_unavailable")   # round 1 set it

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend("SKIP_PLAN — one-line diff")):
        assert await orch._generate_plan(t, GitRepo(bare_repo)) == ""

    assert "plan_unavailable" not in (t.context or {})
    assert "plan_unavailable" not in (await store.get_task(t.id)).context
    assert "NO IMPLEMENTATION PLAN" not in orch._build_implement_prompt(t)


async def test_planner_non_turn_failure_is_not_retried(
    bare_repo, tmp_path, store,
):
    """The retry exists for turn starvation. Any other terminal failure gets
    one call and the same honest no-plan notice — never a second Opus run."""
    cfg = _r3_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=[].append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    backend = ScriptedPlannerBackend(AgentResult(
        final_text="Claude Code returned an error result: transport closed",
        num_turns=2, is_error=True, tokens_used=10, session_id="s",
        stop_reason="error"))
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert plan == ""
    assert backend.budgets == [24]
    assert "NO IMPLEMENTATION PLAN" in orch._build_implement_prompt(t)


async def test_planning_happy_path_is_one_call_and_carries_no_notice(
    bare_repo, tmp_path, store,
):
    """R3 must be byte-invisible when the planner succeeds: one planner call at
    the configured budget, and a coder prompt with the plan and no notice."""
    cfg = _r3_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=[].append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    backend = ScriptedPlannerBackend(
        AgentResult(final_text=_SAMPLE_PLAN, num_turns=5, is_error=False,
                    tokens_used=200, session_id="s", stop_reason="end_turn"))
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        plan = await orch._generate_plan(t, GitRepo(bare_repo))
    await orch._persist_plan(t, plan)

    assert backend.budgets == [24]
    prompt = orch._build_implement_prompt(t)
    assert "IMPLEMENTATION PLAN (follow this plan closely" in prompt
    assert "NO IMPLEMENTATION PLAN" not in prompt


def _scripted_normal_path_query(*messages):
    """Stands in for `claude_backend.query` on the NORMAL path — yields a
    scripted `ResultMessage` per call (never raises) and records the turn
    budget `no_human.core.orchestrator.ClaudeBackend`'s real class passed
    each time, the seam this gap's retry has to be asserted at."""
    budgets = []

    async def _q(*args, **kwargs):
        budgets.append(kwargs["options"].max_turns)
        idx = min(len(budgets) - 1, len(messages) - 1)
        yield messages[idx]

    _q.budgets = budgets
    return _q


@pytest.mark.real_backend
async def test_normal_path_planner_exhaustion_retries_once_at_double_turns(
    bare_repo, tmp_path, store, monkeypatch,
):
    """The gap this closes: a max-turns *ResultMessage* on the normal path
    (no exception) must drive the same one-retry-at-double-turns discipline
    the exception path already has."""
    from claude_agent_sdk import ResultMessage
    from no_human.agent import claude_backend

    cfg = _r3_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    exhausted = ResultMessage(
        subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=24, session_id="s1",
        result="Reached maximum number of turns (24)",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    success = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=10, session_id="s2",
        result=_SAMPLE_PLAN,
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    query_fn = _scripted_normal_path_query(exhausted, success)
    monkeypatch.setattr(claude_backend, "query", query_fn)

    plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert query_fn.budgets == [24, 48], query_fn.budgets
    assert plan == _SAMPLE_PLAN.strip()
    assert not (t.context or {}).get("plan_unavailable")
    texts = [e.get("text", "") for e in events if e.get("kind") == "planning"]
    assert any("retrying once at 48" in x for x in texts), texts


@pytest.mark.real_backend
async def test_normal_path_exhausted_twice_tells_the_coder_there_is_no_plan(
    bare_repo, tmp_path, store, monkeypatch,
):
    from claude_agent_sdk import ResultMessage
    from no_human.agent import claude_backend

    cfg = _r3_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=[].append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    def _exhausted_msg(turns, session):
        return ResultMessage(
            subtype="error_max_turns", duration_ms=1, duration_api_ms=1,
            is_error=True, num_turns=turns, session_id=session,
            result=f"Reached maximum number of turns ({turns})",
            usage={"input_tokens": 100, "output_tokens": 50},
        )

    query_fn = _scripted_normal_path_query(
        _exhausted_msg(24, "s1"), _exhausted_msg(48, "s2"))
    monkeypatch.setattr(claude_backend, "query", query_fn)

    plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert plan == ""
    assert query_fn.budgets == [24, 48], "exactly one retry, no new loop"
    assert "ran out of turns twice (24 then 48)" in orch._build_implement_prompt(t)


@pytest.mark.real_backend
async def test_normal_path_is_error_false_truncation_still_retries(
    bare_repo, tmp_path, store, monkeypatch,
):
    """R17's shape, mirrored in the planner: the SDK does not always mark a
    cut-off round as an error — a `ResultMessage` can carry a truncation
    `stop_reason` (set directly by the SDK, independent of `subtype`) with
    `is_error=False`. `_planner_result_failed` used to bail at the first
    `is_error` check, so this exact shape walked past the retry and its
    cut-off fragment was taken as the plan. It must be discarded and
    retried, the same as the `is_error=True` case."""
    from claude_agent_sdk import ResultMessage
    from no_human.agent import claude_backend

    cfg = _r3_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=[].append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    truncated = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=24, session_id="s1", stop_reason="max_turns",
        result="...cut off mid-plan",
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    success = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=10, session_id="s2",
        result=_SAMPLE_PLAN,
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    query_fn = _scripted_normal_path_query(truncated, success)
    monkeypatch.setattr(claude_backend, "query", query_fn)

    plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert query_fn.budgets == [24, 48], query_fn.budgets
    assert plan == _SAMPLE_PLAN.strip()
    assert not (t.context or {}).get("plan_unavailable")


@pytest.mark.real_backend
async def test_normal_path_max_tokens_truncation_is_discarded_not_read_as_plan(
    bare_repo, tmp_path, store, monkeypatch,
):
    """`max_tokens` is truncation too (R17's tuple), but the retry POLICY is
    unchanged — doubling TURNS does not fix an output-token ceiling, so only
    `max_turns` gets the doubling retry (`result.stop_reason != "max_turns"`
    at the retry gate, untouched by this fix). What this pins is narrower and
    is the actual gap: the cut-off fragment must never be read as the plan,
    `is_error=False` or not. `result` carries `_SAMPLE_PLAN`'s own well-formed
    sections — a plan `_plan_is_unusable` alone would happily accept — so
    only the widened `_planner_result_failed` gate stands between this
    fragment and the coder."""
    from claude_agent_sdk import ResultMessage
    from no_human.agent import claude_backend

    cfg = _r3_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=[].append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    truncated = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=24, session_id="s1", stop_reason="max_tokens",
        result=_SAMPLE_PLAN,
        usage={"input_tokens": 100, "output_tokens": 50},
    )
    query_fn = _scripted_normal_path_query(truncated)
    monkeypatch.setattr(claude_backend, "query", query_fn)

    plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert plan == ""
    assert plan != _SAMPLE_PLAN.strip()
    assert (t.context or {}).get("plan_unavailable")


async def test_planner_quota_wall_raises_instead_of_planning_blind(
    bare_repo, tmp_path, store,
):
    """A planner that dies on the billing wall parks the task, exactly as the
    coder path does — proceeding plan-less would just hit the same wall."""
    from no_human.core.bounds import QuotaExhausted

    cfg = _single_planner_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=[].append)
    t = Task.new("add mul()", repo_path=str(bare_repo))

    quota_text = ("Claude Code returned an error result: You've hit your "
                  "monthly spend limit")
    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=ErroringPlannerBackend(quota_text, "error")):
        with pytest.raises(QuotaExhausted) as exc:
            await orch._generate_plan(t, GitRepo(bare_repo))
    assert "spend limit" in str(exc.value)


async def test_quota_from_the_planning_step_parks_the_task(
    bare_repo, tmp_path, store,
):
    """Planning runs BEFORE the first attempt exists, so the attempt loop's own
    QuotaExhausted handler cannot see it — `_drive_watched` parks it."""
    from no_human.core.bounds import QuotaExhausted

    cfg = _single_planner_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=[].append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)
    # The state `_drive` is actually in when the planner runs (it sets CONTEXT
    # then PLANNING before the first planning call) — an ACTIVE state, from
    # which PAUSED_QUOTA is a legal off-ramp.
    await store.set_status(t, TaskStatus.CONTEXT)
    await store.set_status(t, TaskStatus.PLANNING)

    async def _boom(self, task, repo):
        raise QuotaExhausted("monthly spend limit")

    with _patch.object(Orchestrator, "_drive", _boom):
        outcome = await orch._drive_watched(t, GitRepo(bare_repo))

    assert outcome.status == TaskStatus.PAUSED_QUOTA
    assert (await store.get_task(t.id)).status == TaskStatus.PAUSED_QUOTA


async def test_planner_prose_with_no_sections_is_dropped(
    bare_repo, tmp_path, store,
):
    """Belt to the is_error gate: a non-error plan that parses to a completely
    empty TaskSpec carries nothing the pipeline can use, but WOULD still be
    inlined as an authoritative plan and empty the scope guard."""
    cfg = _single_planner_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))

    prose = "I looked at the repo but could not determine what to change."
    from no_human.core.task import TaskSpec
    spec = TaskSpec.from_plan(prose)
    assert not (spec.files_to_change or spec.approach or spec.test_plan)

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(prose)):
        plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert plan == ""
    planning = [e for e in events if e.get("kind") == "planning"]
    assert any("unusable" in e.get("text", "") for e in planning), planning


async def test_healthy_plan_still_flows_through_untouched(
    bare_repo, tmp_path, store,
):
    """Negative control for the two gates above: a normal multi-section plan
    from a non-error result is returned, persisted, and reaches the coder."""
    cfg = _single_planner_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(_SAMPLE_PLAN)):
        plan = await orch._generate_plan(t, GitRepo(bare_repo))

    assert plan == _SAMPLE_PLAN.strip()
    await orch._persist_plan(t, plan)
    assert (t.context or {})["plan"] == _SAMPLE_PLAN.strip()
    assert (t.context or {})["spec"]["files_to_change"] == ["calc.py: add mul()"]
    prompt = orch._build_implement_prompt(t)
    assert "IMPLEMENTATION PLAN" in prompt
    assert "Do not rename existing functions." in prompt
    planning = [e for e in events if e.get("kind") == "planning"]
    assert any("plan generated" in e.get("text", "") for e in planning)
    assert not any("unusable" in e.get("text", "") for e in planning)


# --------------------------------------------------------------------------- #
# B1: MoA (Mixture-of-Agents) planning fan-out — on by default.               #
# --------------------------------------------------------------------------- #

class MoAFakeBackend:
    """Stands in for every ClaudeBackend(...) construction during one MoA
    plan generation (proposers + aggregator all route through the same
    patched instance). Scripted by inspecting each incoming prompt: a
    "LENS (name)" marker identifies a proposer call, an "=== PROPOSAL ("
    marker identifies the aggregator call; anything else is the
    single-proposer fallback path."""

    def __init__(self, proposals: dict[str, str] | None = None,
                 aggregate_text: str = "", fail_lenses: set[str] | None = None,
                 single_path_text: str = "",
                 error_lenses: set[str] | None = None,
                 aggregate_is_error: bool = False):
        self.proposals = proposals or {}
        self.aggregate_text = aggregate_text
        self.fail_lenses = fail_lenses or set()
        # `fail_lenses` RAISES; `error_lenses` returns the terminal-error result
        # the SDK actually hands back (is_error=True, the error string as text).
        self.error_lenses = error_lenses or set()
        self.aggregate_is_error = aggregate_is_error
        self.single_path_text = single_path_text
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.prompts.append(prompt)
        for lens_name in self.fail_lenses:
            if f"LENS ({lens_name})" in prompt:
                raise RuntimeError(f"simulated failure for {lens_name}")
        for lens_name in self.error_lenses:
            if f"LENS ({lens_name})" in prompt:
                return AgentResult(final_text=_MAX_TURNS_ERROR, num_turns=10,
                                   is_error=True, tokens_used=1234,
                                   session_id="s", stop_reason="max_turns")
        if "=== PROPOSAL (" in prompt and self.aggregate_is_error:
            return AgentResult(final_text=_MAX_TURNS_ERROR, num_turns=10,
                               is_error=True, tokens_used=1234,
                               session_id="s", stop_reason="max_turns")
        for lens_name, text in self.proposals.items():
            if f"LENS ({lens_name})" in prompt:
                return AgentResult(final_text=text, num_turns=2, is_error=False,
                                   tokens_used=50, session_id="s", stop_reason="end_turn")
        if "=== PROPOSAL (" in prompt:
            return AgentResult(final_text=self.aggregate_text, num_turns=3,
                               is_error=False, tokens_used=100, session_id="s",
                               stop_reason="end_turn")
        return AgentResult(final_text=self.single_path_text, num_turns=1,
                           is_error=False, tokens_used=20, session_id="s",
                           stop_reason="end_turn")


def _moa_config(tmp_path, **overrides):
    """Config for tests of MoA *mechanics*. `min_signals=0` fans out
    unconditionally, so these tests exercise the proposer/aggregator path rather
    than the B2 complexity gate (which has its own tests below)."""
    cfg = _planning_config(tmp_path)
    cfg.data["llm"]["moa_planning"] = {
        "enabled": True, "proposers": 3, "min_signals": 0, **overrides,
    }
    return cfg


async def test_moa_planning_enabled_by_default():
    """The whole point of building MoA planning was for it to actually run —
    it must be on, not sitting inert behind an opt-in flag nobody sets."""
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["llm"]["moa_planning"]["enabled"] is True


async def test_moa_planning_synthesizes_proposals(bare_repo, tmp_path, store):
    """3 proposers + 1 aggregator, all through the same patched backend;
    the aggregator's output is what _generate_plan returns."""
    cfg = _moa_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={
            "minimal-first": _SAMPLE_PLAN,
            "risk-first": _SAMPLE_PLAN.replace("mul", "mul_edge"),
            "test-first": _SAMPLE_PLAN.replace("test_mul", "test_mul_first"),
        },
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == "## FILES TO CHANGE/CREATE\n- calc.py: synthesized"
    assert len(fake.prompts) == 4  # 3 proposers + 1 aggregator, no fallback
    moa_events = [e for e in events if e.get("kind") == "planning_moa"]
    assert any("synthesized" in e.get("text", "") for e in moa_events)
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("plan generated" in e.get("text", "") for e in planning_events)


async def test_moa_errored_proposer_is_excluded_from_the_drafts(
    bare_repo, tmp_path, store,
):
    """A proposer that ends on a terminal SDK error is a FAILED draft — its
    error string must never be handed to the aggregator as a proposal."""
    cfg = _moa_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={
            "minimal-first": _SAMPLE_PLAN,
            "test-first": _SAMPLE_PLAN.replace("test_mul", "test_mul_first"),
        },
        error_lenses={"risk-first"},
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    # The two healthy proposers still carry the plan through.
    assert result == "## FILES TO CHANGE/CREATE\n- calc.py: synthesized"
    agg_prompt = next(p for p in fake.prompts if "=== PROPOSAL (" in p)
    assert "=== PROPOSAL (risk-first) ===" not in agg_prompt
    assert _MAX_TURNS_ERROR not in agg_prompt
    assert "=== PROPOSAL (minimal-first) ===" in agg_prompt
    moa = [e for e in events if e.get("kind") == "planning_moa"]
    assert any("risk-first" in e.get("text", "") and "max_turns" in e.get("text", "")
               for e in moa), moa


async def test_moa_errored_aggregator_falls_back_to_a_proposal(
    bare_repo, tmp_path, store,
):
    """An aggregator that errors out takes the existing "produced no output"
    fallback — never ships its error string as the synthesized plan."""
    cfg = _moa_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={
            "minimal-first": _SAMPLE_PLAN,
            "risk-first": _SAMPLE_PLAN.replace("mul", "mul_edge"),
            "test-first": _SAMPLE_PLAN.replace("test_mul", "test_mul_first"),
        },
        aggregate_is_error=True,
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == _SAMPLE_PLAN.strip()   # drafts[0] — the first proposal
    moa = [e for e in events if e.get("kind") == "planning_moa"]
    assert any("using first proposal" in e.get("text", "") for e in moa), moa


async def test_moa_announces_the_fan_out_and_attributes_each_lens(
    bare_repo, tmp_path, store,
):
    """The fan-out used to emit nothing until synthesis, minutes later, and the
    three proposers' events were indistinguishable from each other."""
    cfg = _moa_config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    moa = [e for e in events if e.get("kind") == "planning_moa"]

    # 1. The fan-out announces itself, names every lens, and names the model.
    fan_out = next(e for e in moa if "fanning out" in e["text"])
    assert fan_out["model"] == cfg.data["llm"]["planner_model"]
    assert fan_out["proposers"] == ["minimal-first", "risk-first", "test-first"]
    for lens in ("minimal-first", "risk-first", "test-first"):
        assert lens in fan_out["text"]

    # 2. Each proposer reports its own completion, tagged with its lens.
    finished = {e["lens"] for e in moa if "finished" in e["text"]}
    assert finished == {"minimal-first", "risk-first", "test-first"}

    # 3. The fan-out is announced before any proposer finishes.
    assert moa.index(fan_out) == 0


# --------------------------------------------------------------------------- #
# B2: the MoA complexity gate                                                  #
# --------------------------------------------------------------------------- #

def _gate_cfg(**overrides):
    from no_human.config import DEFAULT_CONFIG
    return {**DEFAULT_CONFIG["llm"]["moa_planning"], **overrides}


def test_moa_signals_none_for_a_trivial_task():
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("fix a typo in the README", repo_path="/r", kind="bugfix")
    assert _moa_complexity_signals(t, _gate_cfg()) == []


def test_moa_signals_for_the_ci_gate_task_shape():
    """The real CI_GATE task (61406d02): kind=feature, 10 acceptance criteria, a
    9309-char description, and — once D19/A2 stages it — one linked repo.
    kind=feature is NOT a signal: every dogfood helper is a feature, so it
    acted as a permanent +1 that let any enriched helper fan out 3 Opus
    proposers (task 6e64c555 live, 2026-07-12)."""
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("Per-PR CI_GATE Integration Test Pipeline", repo_path="/r",
                 kind="feature", description="x" * 9309)
    t.acceptance_criteria = [f"criterion {i}" for i in range(10)]
    t.linked_repos = ["/repos/metrics-core-service"]
    assert set(_moa_complexity_signals(t, _gate_cfg())) == {
        "multi-repo", "many-criteria", "long-spec",
    }


def test_moa_counts_operator_criteria_not_enriched_ones():
    """Intake enrichment turned 2 operator criteria into 10 on a kebab-case
    helper, which tripped many-criteria and fanned out 3 Opus proposers
    (917k cache-read of planning on a trivial task, measured live). The gate
    must count the criteria the OPERATOR stated, preserved by _act_on_eval
    in context['original_criteria']."""
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("Add a kebabCase helper", repo_path="/r", kind="feature")
    t.acceptance_criteria = [f"enriched {i}" for i in range(10)]
    t.context = {"original_criteria": ["converts inputs", "tests pass"]}
    assert _moa_complexity_signals(t, _gate_cfg()) == []


def test_moa_feature_kind_alone_is_not_complexity():
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("Add a helper", repo_path="/r", kind="feature")
    t.acceptance_criteria = ["works", "tested"]
    assert _moa_complexity_signals(t, _gate_cfg()) == []


def test_moa_signals_read_the_evaluator_verdict():
    from no_human.core.orchestrator import _moa_complexity_signals
    t = Task.new("t", repo_path="/r", kind="bugfix")
    t.context = {"eval_result": {"verdict": "clarify"}}
    assert _moa_complexity_signals(t, _gate_cfg()) == ["ambiguous-spec"]


async def test_moa_gate_skips_the_fan_out_for_a_simple_task(
    bare_repo, tmp_path, store
):
    """No signals fire for a bare task (kind=feature is deliberately not a
    signal), so it takes the single-planner path instead of paying for three
    Opus proposers."""
    cfg = _planning_config(tmp_path)  # default moa_planning: min_signals=2
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("small change", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert len(backend.prompts) == 1                    # single planner, no fan-out
    assert not [e for e in events if e.get("kind") == "planning_moa"]
    gate = next(e for e in events if "MoA gate" in e.get("text", ""))
    assert gate["signals"] == []
    assert "single planner" in gate["text"]


async def test_moa_gate_fans_out_for_a_complex_task(bare_repo, tmp_path, store):
    """Two signals (many-criteria + long-spec) meet the bar: 3 proposers + 1
    aggregator."""
    cfg = _planning_config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("big change", repo_path=str(bare_repo),
                 description="x" * 2500)
    t.acceptance_criteria = [f"criterion {i}" for i in range(6)]
    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert len(fake.prompts) == 4                       # 3 proposers + aggregator
    gate = next(e for e in events if "MoA gate" in e.get("text", ""))
    assert set(gate["signals"]) == {"long-spec", "many-criteria"}


async def test_min_signals_zero_restores_unconditional_moa(
    bare_repo, tmp_path, store
):
    """The documented escape hatch: min_signals=0 is the pre-B2 behavior."""
    cfg = _moa_config(tmp_path)  # pins min_signals=0
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("trivial", repo_path=str(bare_repo), kind="bugfix")
    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
    )

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    assert len(fake.prompts) == 4  # fans out despite zero complexity signals


async def test_moa_reports_a_failed_proposer_by_lens(bare_repo, tmp_path, store):
    cfg = _moa_config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text="## FILES TO CHANGE/CREATE\n- calc.py: synthesized\n",
        fail_lenses={"risk-first"},
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    moa = [e for e in events if e.get("kind") == "planning_moa"]
    failed = [e for e in moa if "failed" in e["text"]]
    assert len(failed) == 1
    assert failed[0]["lens"] == "risk-first"


async def test_moa_aggregator_prompt_forbids_numeric_score(bare_repo, tmp_path, store):
    """Review and synthesis are evidence-based, never a numeric self-score.
    The aggregator prompt must not invite one."""
    import re as _re
    cfg = _moa_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN, "risk-first": _SAMPLE_PLAN,
                   "test-first": _SAMPLE_PLAN},
        aggregate_text=_SAMPLE_PLAN,
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        await orch._generate_plan(t, GitRepo(bare_repo))

    agg_prompt = fake.prompts[-1]
    assert "=== PROPOSAL (" in agg_prompt
    assert "numeric score" in agg_prompt.lower()
    assert not _re.search(r"score\s+\d+\s*[-–]\s*10", agg_prompt, _re.IGNORECASE)


async def test_moa_planning_falls_back_on_insufficient_proposers(bare_repo, tmp_path, store):
    """2 of 3 proposers fail → too few for a meaningful synthesis → falls
    back to the normal single-proposer path (same patched backend)."""
    cfg = _moa_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    fake = MoAFakeBackend(
        proposals={"minimal-first": _SAMPLE_PLAN},
        fail_lenses={"risk-first", "test-first"},
        single_path_text=_SAMPLE_PLAN,
    )
    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=fake):
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == _SAMPLE_PLAN.strip()
    moa_events = [e for e in events if e.get("kind") == "planning_moa"]
    assert any("falling back" in e.get("text", "") for e in moa_events)
    planning_events = [e for e in events if e.get("kind") == "planning"]
    assert any("plan generated" in e.get("text", "") for e in planning_events)


async def test_moa_planning_can_be_disabled(bare_repo, tmp_path, store):
    """Explicitly opting out reverts to exactly one planner call."""
    cfg = _planning_config(tmp_path)
    cfg.data["llm"]["moa_planning"] = {"enabled": False}
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(_SAMPLE_PLAN)) as mocked:
        result = await orch._generate_plan(t, GitRepo(bare_repo))

    assert result == _SAMPLE_PLAN.strip()
    assert mocked.call_count == 1


async def test_plan_injected_into_implement_prompt(bare_repo, tmp_path, store):
    """Plan from task.context is injected into the implement prompt."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    t.context = {"plan": "## FILES TO CHANGE\n- calc.py: add mul()"}

    prompt = orch._build_implement_prompt(t)

    assert "IMPLEMENTATION PLAN" in prompt
    assert "calc.py: add mul()" in prompt
    assert "OUT OF SCOPE" in prompt  # the instruction about respecting scope


async def test_a_long_plan_inlines_only_its_head(bare_repo, tmp_path, store):
    """Transcript diet (M3): an inlined plan is cache-read on EVERY turn of
    the session. Past the threshold only the head inlines; the coder is told
    to read .no_human/PLAN.md first and grep it selectively."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("big refactor", repo_path=str(bare_repo))
    t.acceptance_criteria = ["x"]
    head = "## OBJECTIVE\nrefactor the flux capacitor\n"
    tail_marker = "UNIQUE-TAIL-SENTINEL"
    t.context = {"plan": head + ("filler line\n" * 800) + tail_marker}
    assert len(t.context["plan"]) > orch._PLAN_INLINE_MAX

    prompt = orch._build_implement_prompt(t)

    assert "READ IT FIRST" in prompt and ".no_human/PLAN.md" in prompt
    assert "flux capacitor" in prompt, "the head must inline for orientation"
    assert tail_marker not in prompt, "the tail must NOT be in every cached turn"
    assert "OUT OF SCOPE" in prompt


async def test_no_plan_no_plan_block_in_prompt(bare_repo, tmp_path, store):
    """Without a plan, the implement prompt has no plan block."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]

    prompt = orch._build_implement_prompt(t)

    assert "IMPLEMENTATION PLAN" not in prompt


async def test_debug_preamble_only_on_retry(bare_repo, tmp_path, store):
    """1.5: a first attempt has no debug preamble (byte-identical); a retry
    (attempt_log present) steers the coder to root-cause via no_human_debug."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]

    first = orch._build_implement_prompt(t)
    assert "no_human_debug" not in first and "A PRIOR ATTEMPT" not in first

    t.context = {"attempt_log": ["attempt 1: tests failed: 0 passed, 1 errors"]}
    retry = orch._build_implement_prompt(t)
    assert "A PRIOR ATTEMPT ON THIS TASK FAILED" in retry
    assert "no_human_debug" in retry and "patch-guess" in retry


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_full_pipeline_with_planning(bare_repo, tmp_path, store):
    """Full pipeline: planning (mocked) → implement (FakeBackend) → PR.

    This is the integration test that proves planning feeds into the implement
    prompt and the full lifecycle completes. All LLM calls are mocked.
    """
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(_SAMPLE_PLAN)):
        outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    # Plan was stored in context
    refreshed = await store.get_task(t.id)
    assert refreshed.context.get("plan") == _SAMPLE_PLAN.strip()
    # Planning event was emitted
    kinds = [e["kind"] for e in events]
    assert "planning" in kinds
    assert outcome.pr_url and "no-human/" in outcome.pr_url


async def test_plan_file_is_scoped_to_no_human_dir_and_cleaned_up(
    bare_repo, tmp_path, store,
):
    """The plan is materialized for the agent, then removed.

    Serial mode has no worktree, so ``repo.path`` is the user's primary
    checkout: a root-level PLAN.md outlives the run and the next task's planner
    reads it back as repo content.
    """
    seen: dict[str, bool] = {}

    def mutate(cwd):
        # Observed from inside the agent session, while the run is live.
        seen["under_no_human"] = (cwd / ".no_human" / "PLAN.md").is_file()
        seen["at_root"] = (cwd / "PLAN.md").exists()
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    with _patch("no_human.core.orchestrator.ClaudeBackend",
                return_value=PlannerBackend(_SAMPLE_PLAN)):
        outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert seen["under_no_human"], "the agent never saw the plan on disk"
    assert not seen["at_root"], "the plan must not be written to the checkout root"
    # Nothing survives the run in either location.
    assert not (bare_repo / ".no_human" / "PLAN.md").exists()
    assert not (bare_repo / "PLAN.md").exists()


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_stale_plan_file_is_removed_when_this_run_has_no_plan(
    bare_repo, tmp_path, store,
):
    """A plan left behind by a crashed run is never inherited by the next one.

    Scoped to `isolation.enabled: false`, because that is the only mode that
    writes the plan into the operator's checkout and so the only mode with
    anything to clean up. The default mode is covered by the test below."""
    stale = bare_repo / ".no_human" / "PLAN.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# a previous run's plan — must not leak into this one\n")

    def mutate(cwd):
        assert not (cwd / ".no_human" / "PLAN.md").exists(), "stale plan visible to agent"
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    cfg = _config(tmp_path)  # planning disabled → no plan for this task
    cfg.data["isolation"]["enabled"] = False
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("tweak add()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    assert not stale.exists()


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_a_stale_plan_in_the_checkout_is_invisible_to_an_isolated_run(
    bare_repo, tmp_path, store,
):
    """Same guarantee, default config: the run happens in its own worktree,
    which is built from a commit — so a `.no_human/PLAN.md` sitting untracked
    in the operator's checkout cannot reach the agent at all, and the run does
    not reach into that checkout to delete it either."""
    stale = bare_repo / ".no_human" / "PLAN.md"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("# a previous run's plan — must not leak into this one\n")

    seen: dict[str, bool] = {}

    def mutate(cwd):
        seen["plan_visible"] = (cwd / ".no_human" / "PLAN.md").exists()
        seen["isolated"] = cwd != bare_repo
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    cfg = _config(tmp_path)  # planning disabled → no plan for this task
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("tweak add()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    assert seen.get("isolated"), "the run used the operator's checkout"
    assert seen.get("plan_visible") is False, "stale plan visible to agent"
    assert stale.read_text().startswith("# a previous run's plan")


# --------------------------------------------------------------------------- #
# Role attribution on agent events (the System view reads `source`)            #
# --------------------------------------------------------------------------- #

def _bare_orch(sink):
    """An Orchestrator with only the event sink wired — enough for _agent_sink."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._sink = sink
    return orch


def test_agent_sink_defaults_to_the_coder_role():
    events: list[dict] = []
    _bare_orch(events.append)._agent_sink(AgentEvent("tool_use", tool_name="Read"))
    assert events[0]["source"] == "agent"
    assert events[0]["tool_name"] == "Read"


def test_sink_for_stamps_the_role_on_every_event():
    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._sink_for("planner:test-first")(AgentEvent("tool_use", tool_name="Grep"))
    orch._sink_for("aggregator")(AgentEvent("text", text="synthesizing"))
    assert [e["source"] for e in events] == ["planner:test-first", "aggregator"]


def test_sink_for_gives_each_concurrent_proposer_its_own_lens():
    """The MoA proposers run under asyncio.gather; a shared _active_role attr
    would hand every one of them whichever lens was assigned last."""
    events: list[dict] = []
    orch = _bare_orch(events.append)
    sinks = [orch._sink_for(f"planner:{lens}")
             for lens in ("minimal-first", "risk-first", "test-first")]
    # Interleave, as concurrent proposers do.
    for s in sinks:
        s(AgentEvent("tool_use", tool_name="Read"))
    for s in reversed(sinks):
        s(AgentEvent("subagent_start", text="Investigate Jenkinsfile structure"))
    assert [e["source"] for e in events] == [
        "planner:minimal-first", "planner:risk-first", "planner:test-first",
        "planner:test-first", "planner:risk-first", "planner:minimal-first",
    ]


def test_planner_tool_calls_do_not_feed_the_implementers_doom_loop_detector():
    """The planner is read-only and runs before the attempt. Its repeated Reads
    must not trip the coder's doom-loop detector, nor land in the edited-file
    set — the worker pool reuses one Orchestrator across tasks."""
    from no_human.core.bounds import StuckDetector

    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()

    planner = orch._sink_for("planner:risk-first")
    for _ in range(5):
        planner(AgentEvent("tool_use", tool_name="Read",
                           tool_input={"file_path": "/src/foo.py"}))
    planner(AgentEvent("tool_use", tool_name="Edit",
                       tool_input={"file_path": "/src/foo.py"}))

    assert not any(e["kind"] == "stuck" for e in events)
    assert not getattr(orch, "_agent_edited_files", set())


# --------------------------------------------------------------------------- #
# Phase 7e: doom-loop detection wired through _agent_sink                      #
# --------------------------------------------------------------------------- #

class DoomLoopBackend:
    """Backend that emits 3 identical Read tool_use events (a doom-loop)."""

    def __init__(self, mutate):
        self.mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        if on_event:
            for _ in range(3):
                on_event(AgentEvent("tool_use", tool_name="Read",
                                    tool_input={"file_path": "/src/foo.py"}))
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=4, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_doom_loop_emits_stuck_event(bare_repo, tmp_path, store):
    """When the agent repeats the exact same tool call 3×, the orchestrator
    emits a 'stuck' event but does NOT interrupt the attempt (constraint #5)."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef greet():\n    return 'hi'\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, greet\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_greet():\n    assert greet() == 'hi'\n"
        )

    cfg = _config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, DoomLoopBackend(mutate),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("trigger doom loop", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # The attempt should still complete — no mid-attempt interruption.
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    # A "stuck" event with doom-loop text must have been emitted.
    stuck_events = [e for e in events if e.get("kind") == "stuck"]
    assert len(stuck_events) >= 1
    assert "doom-loop" in stuck_events[0]["text"]


# --------------------------------------------------------------------------- #
# D18: drafts in agent-owned dirs must not trip the per-file edit-loop         #
# --------------------------------------------------------------------------- #

def _draft_then_read(orch, path: str) -> None:
    """Write *path* 5× (edit_threshold), interleaving distinct reads.

    The interleaving keeps the doom-loop detector (3× identical consecutive
    signature) and the ping-pong detector (A-B-A-B) quiet, so the only detector
    under test is the per-file edit count.
    """
    for i in range(5):
        orch._agent_sink(AgentEvent("tool_use", tool_name="Write",
                                    tool_input={"file_path": path}))
        orch._agent_sink(AgentEvent("tool_use", tool_name="Read",
                                    tool_input={"file_path": f"/repo/src/m{i}.py"}))


def test_drafts_in_agent_owned_dirs_do_not_trip_the_edit_loop():
    """Task 61406d02 died here: the coder drafted in `.no_human/`, the scope
    guard told it to "revert and stay within the planned file list", and the
    rewrite tripped the edit-loop. `.no_human/` is excluded from every git diff,
    so those writes are neither committable nor a doom signal.
    """
    from no_human.core.bounds import StuckDetector

    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()
    _draft_then_read(orch, "/repo/.no_human/ci_gate_stage_draft.groovy")

    assert not [e for e in events if e.get("kind") == "stuck"]
    assert not getattr(orch, "_agent_edited_files", set())


def test_a_worktree_is_not_mistaken_for_an_agent_owned_dir():
    """Concurrency worktrees live at ~/.no_human/worktrees/<task_id>, so EVERY
    source file inside one has a `.no_human` component in its absolute path.
    Without the repo root to strip, `is_agent_owned` swallows the whole worktree:
    `_agent_edited_files` stays empty (so the commit degrades from commit_paths to
    commit_all) and the edit-loop detector never counts a thing."""
    from no_human.core.bounds import StuckDetector

    worktree = "/Users/u/.no_human/worktrees/abc123"
    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()
    orch._active_repo_root = worktree
    _draft_then_read(orch, f"{worktree}/src/calc.py")

    stuck = [e for e in events if e.get("kind") == "stuck"]
    assert len(stuck) == 1
    assert "edit-loop" in stuck[0]["text"]
    assert orch._agent_edited_files == {f"{worktree}/src/calc.py"}


def test_scratch_inside_a_worktree_is_still_agent_owned():
    """…while a genuine `.no_human/scratch/` *inside* the worktree stays exempt."""
    from no_human.core.bounds import StuckDetector

    worktree = "/Users/u/.no_human/worktrees/abc123"
    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()
    orch._active_repo_root = worktree
    _draft_then_read(orch, f"{worktree}/.no_human/scratch/draft.groovy")

    assert not [e for e in events if e.get("kind") == "stuck"]
    assert not getattr(orch, "_agent_edited_files", set())


def test_repeated_edits_to_a_real_file_still_trip_the_edit_loop():
    """Positive control for the exemption above — a real source file must still
    be caught, otherwise the D18 fix silently disables edit-loop detection."""
    from no_human.core.bounds import StuckDetector

    events: list[dict] = []
    orch = _bare_orch(events.append)
    orch._stuck = StuckDetector()
    _draft_then_read(orch, "/repo/src/calc.py")

    stuck = [e for e in events if e.get("kind") == "stuck"]
    assert len(stuck) == 1
    assert "edit-loop" in stuck[0]["text"]
    assert orch._agent_edited_files == {"/repo/src/calc.py"}


async def test_unstageable_linked_repo_is_announced_not_swallowed(
    bare_repo, tmp_path, store
):
    """D19: a linked repo that is not a git checkout used to be dropped by a bare
    `continue` — no event, no log the board could show. The planner still named
    its files, and nothing there could ever be committed. It stays non-fatal (the
    primary repo's work is worth doing) but it must be visible."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    missing = tmp_path / "metrics-core-service-not-a-checkout"
    missing.mkdir()

    cfg = _config(tmp_path)
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("multi-repo task", repo_path=str(bare_repo))
    t.linked_repos = [str(missing)]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL  # non-fatal
    announced = [e for e in events if e.get("kind") == "linked_repo"]
    assert len(announced) == 1
    assert announced[0]["ok"] is False
    assert str(missing) in announced[0]["text"]
    assert "not a git checkout" in announced[0]["text"]


class DoomLoopThenFailBackend:
    """Doom-loops on every attempt, then hits max_turns without ever fixing
    anything — so the stuck signal must survive into the failure detail."""

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        if on_event:
            for _ in range(3):
                on_event(AgentEvent("tool_use", tool_name="Read",
                                    tool_input={"file_path": "/src/foo.py"}))
            on_event(AgentEvent("result", text="Reached maximum number of turns (40)"))
        return AgentResult(
            final_text="Reached maximum number of turns (40)",
            num_turns=max_turns, is_error=True, tokens_used=100,
            session_id="s", stop_reason="max_turns",
        )


async def test_a_failed_attempt_writes_the_fix_pair_ledger(bare_repo, tmp_path, store):
    """The CALL SITE, not the method. Deleting the one line that invokes
    `_record_and_lookup_fix_pair` from the failure path left every fix-pair
    test green — the method was covered, its invocation was not, and a feature
    nothing calls is a feature that does not exist.

    So: run a task that actually fails, and require the ledger to carry a row
    for it afterwards."""
    from no_human.core.bounds import error_signature  # noqa: F401 (documents the key)

    cfg = _config(tmp_path)
    backend = DoomLoopThenFailBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=lambda e: None)
    t = Task.new("fail so the ledger is written", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    rows = await store._fetchall(
        "SELECT sig, task_id, error_excerpt, resolution FROM fix_pairs "
        "WHERE task_id = ?", (t.id,))
    assert rows, "a failed attempt recorded no friction — the seam is not wired"
    # open friction, not a fix pair: this task never succeeded
    assert all(r["resolution"] is None for r in rows)
    assert all(r["error_excerpt"] for r in rows)


async def test_doom_loop_reason_persists_into_attempt_log(bare_repo, tmp_path, store):
    """A doom-loop mid-attempt must change what the NEXT attempt is told —
    otherwise the 'stuck: resetting context' claim is just telemetry (the
    audited gap). The reason should land in failure_reason (stored per
    attempt) and in task.context['attempt_log'] (fed into the next attempt's
    resume digest by _resume_digest)."""
    cfg = _config(tmp_path)
    backend = DoomLoopThenFailBackend()
    events: list[dict] = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("trigger doom loop then fail", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    attempts = await store.list_attempts(t.id)
    # W4 (failed-restoration fingerprint): both attempts end in a
    # byte-equivalent state, so the SECOND identical failure stops the loop —
    # the third attempt would re-prove the same wall at the 20×-cost tier.
    assert len(attempts) == 2
    assert all("doom-loop" in (a.get("failure_reason") or "") for a in attempts)
    assert t.context.get("attempt_log")
    assert any("doom-loop" in entry for entry in t.context["attempt_log"])


# --------------------------------------------------------------------------- #
# Investigation tasks that produce findings but no code changes should         #
# complete as DONE with a report, not FAILED.                                  #
# --------------------------------------------------------------------------- #

class ReportOnlyBackend:
    """Backend that returns findings text but makes no file changes."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(
            final_text="Root cause: the medstarhr instance stopped sending events "
                       "at 2026-07-04T18:00Z due to a misconfigured retention policy.",
            num_turns=5, is_error=False, tokens_used=500,
            session_id="s", stop_reason="end_turn",
        )


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_investigation_report_only_completes_as_done(bare_repo, tmp_path, store):
    """An investigation task that produces findings but no file changes → DONE."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, ReportOnlyBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("investigate data drop", repo_path=str(bare_repo),
                 kind="investigation")
    t.acceptance_criteria = ["identify root cause of data drop"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE, f"expected DONE, got {outcome.status}"
    assert "report-only" in outcome.detail
    # v7 live find: the judge was fed the placeholder detail because the DONE
    # outcome never carried the findings — the #85 bug class on this terminal.
    # The deliverable must ride on outcome.report, like every other producer.
    assert "medstarhr" in outcome.report
    # Findings stored in task context
    refreshed = await store.find_task(t.id)
    assert "findings" in (refreshed.context or {})
    assert "medstarhr" in refreshed.context["findings"]
    # Attempt marked succeeded, not failed
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "succeeded"
    # investigation_report event emitted
    kinds = [e["kind"] for e in events]
    assert "investigation_report" in kinds


async def test_investigation_with_code_changes_follows_normal_path(bare_repo, tmp_path, store):
    """An investigation that also fixes the bug should follow the normal commit→PR flow."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("investigate and fix bug", repo_path=str(bare_repo),
                 kind="investigation")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Should follow normal PR flow, not the report-only path
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url and "no-human/" in outcome.pr_url


async def test_non_investigation_no_changes_still_fails(bare_repo, tmp_path, store):
    """A feature task with no file changes should still FAIL (not get the report path)."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, ReportOnlyBackend(), SlackNotifier(None))
    t = Task.new("add feature", repo_path=str(bare_repo), kind="feature")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # After exhausting max_attempts with no changes, the orchestrator escalates.
    assert outcome.status in (TaskStatus.FAILED, TaskStatus.ESCALATED)


# --------------------------------------------------------------------------- #
# A3: the zero-diff attempt breaker                                            #
# --------------------------------------------------------------------------- #

#: Backends that can continue a session declare it, exactly as the real ones do
#: (`BackendCapabilities.session_resume`). The reformat nudge FAILS CLOSED on
#: this: a fake without it gets no nudge at all, which is the point — a fake
#: that has not said it can resume must not be handed a `resume=` argument.
RESUMABLE = _SimpleNamespace(name="fake", session_resume=True)


class ZeroDiffBackend:
    """Edits nothing and says the work is already done — verbatim in spirit to
    what task d9d458b5's agent said on all three of its attempts."""

    STATEMENT = ("The implementation is already complete. I made zero edits. "
                 "I did not need to fabricate changes — doing so would violate "
                 "the 'smallest change' rule.")
    capabilities = RESUMABLE

    def __init__(self):
        self.prompts: list[str] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.prompts.append(prompt)
        return AgentResult(final_text=self.STATEMENT, num_turns=5, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_two_zero_diff_attempts_escalate_with_the_agents_reason(
    bare_repo, tmp_path, store
):
    """d9d458b5 burned 3 attempts × ~18 turns re-running an agent against a repo
    it never modified, then escalated with 'agent produced no file changes' ×3 —
    while the agent's actual reason ("already complete, I won't fabricate") was
    discarded. Two attempts, then escalate carrying that reason."""
    cfg = _config(tmp_path)
    backend = ZeroDiffBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add feature", repo_path=str(bare_repo), kind="feature")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    # bounds.max_attempts is 3 — the third never ran. Each of the two attempts
    # also spends its ONE reformat nudge (this report does not parse either),
    # so the ATTEMPT prompts are the non-nudge ones.
    coder_prompts = [p for p in backend.prompts
                     if _REFORMAT_NUDGE_MARKER not in p]
    assert len(coder_prompts) == 2
    assert len(backend.prompts) == 4
    assert len(await store.list_attempts(t.id)) == 2

    blocker = outcome.task.blocker
    assert blocker["category"] == "AMBIGUITY"
    assert "fabricate changes" in blocker["evidence"]
    assert blocker["tried"]  # the per-attempt log, for the human reading it


async def test_work_already_committed_on_the_branch_is_not_zero_diff(
    bare_repo, tmp_path, store
):
    """`nh reply` (D15) resumes from a [WIP-BLOCKED] checkpoint whose work is
    already committed. The agent correctly adds nothing, but `has_changes()` only
    sees the working tree, so the attempt was failed as "agent produced no file
    changes". Task 84251cb2 had 645 lines committed against dev and was killed for
    it twice. The change is the branch's diff against base — ask git."""
    from no_human.vcs import GitRepo

    repo = GitRepo(bare_repo)
    base = repo.current_branch()
    repo.create_branch("scratch/resumed", base=base)
    (bare_repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    (bare_repo / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n")
    committed = repo.commit_all("WIP-BLOCKED: prior attempt's work")

    assert not repo.has_changes()               # the tree is clean…
    assert repo.commits_ahead(base) == 1        # …but the branch carries the work
    head = repo.head_commit(base)
    assert head.sha == committed.sha
    assert head.files_changed == 2 and head.insertions > 0

    # Restore the checkout; the checkpoint commit stays reachable by sha.
    repo._run("checkout", base)


async def test_a_resumed_attempt_reviews_the_checkpoint_instead_of_failing(
    bare_repo, tmp_path, store
):
    """End-to-end: `nh reply` sets context['resume_from'], so the attempt branches
    from the [WIP-BLOCKED] commit. The agent adds nothing (there is nothing to
    add), `has_changes()` is False, and the attempt used to die as "agent produced
    no file changes" — twice, then escalate. It must instead review what is on the
    branch and reach a PR."""
    from no_human.vcs import GitRepo

    repo = GitRepo(bare_repo)
    base = repo.current_branch()
    repo.create_branch("scratch/checkpoint", base=base)
    (bare_repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    (bare_repo / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n")
    checkpoint = repo.commit_all("[WIP-BLOCKED] prior attempt's work")
    repo._run("checkout", base)

    cfg = _config(tmp_path)
    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("mul implemented", True, "calc.py:4 returns a*b")],
    ))
    # A backend that edits nothing — exactly what a resumed coder does.
    orch = Orchestrator(store, cfg.data, ZeroDiffBackend(), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.context = {"resume_from": {"sha": checkpoint.sha}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert not [e for e in events if e.get("kind") == "attempt_failed"]
    resumed = [e for e in events if e.get("kind") == "commit" and e.get("resumed")]
    assert len(resumed) == 1
    assert resumed[0]["files_changed"] == 2


async def test_zero_diff_preamble_appears_on_retry_and_forbids_fabrication(
    bare_repo, tmp_path, store
):
    """The corrective preamble must name the valid outcomes (edit /
    already-satisfied report / blocker) without implying an edit has to appear
    — the agent it addresses may well be right."""
    cfg = _config(tmp_path)
    backend = ZeroDiffBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add feature", repo_path=str(bare_repo), kind="feature")
    await store.create_task(t)

    await orch.run_task(t)

    # The two ATTEMPT prompts; each attempt also spends one reformat nudge,
    # which is a follow-up on the same session and carries no preamble.
    first, second = [p for p in backend.prompts
                     if _REFORMAT_NUDGE_MARKER not in p]
    assert "FINISHED WITHOUT EDITING ANY FILE" not in first
    assert "FINISHED WITHOUT EDITING ANY FILE" in second
    assert "Do NOT invent an edit" in second
    # And every attempt offers the sanctioned "already satisfied" exit, so an
    # agent that is right can say so on attempt 1 instead of looking like a stall.
    for prompt in (first, second):
        assert "ALREADY satisfied by the existing code" in prompt
        assert "not a silent no-op" in prompt
        # …and it must not become an escape hatch from work the agent can do.
        assert "avoid finishing doable work" in prompt


# --------------------------------------------------------------------------- #
# The already-satisfied zero-diff terminal                                     #
# --------------------------------------------------------------------------- #

class AlreadySatisfiedBackend:
    """Zero edits + a fully-cited ALREADY-SATISFIED per-criterion claim."""

    STATEMENT = (
        "Verified every criterion against the existing code.\n"
        "ALREADY-SATISFIED\n"
        "CRITERION: mul(a,b) returns product — MET — evidence: calc.py:4\n"
    )

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(final_text=self.STATEMENT, num_turns=3, is_error=False,
                           tokens_used=120, session_id="s", stop_reason="end_turn")


async def test_zero_diff_already_satisfied_claim_reaches_the_human_gate(
    bare_repo, tmp_path, store
):
    """The v6 'answer in hand' park class (ns-b682d728, ns-deb7de00,
    ns-01c3d46d): a task whose criteria are already true in the code must reach
    the human gate carrying the evidence-cited claim as its deliverable — via
    the fresh-context reviewer, never on the coder's word."""
    passing = ReviewDecision(passed=True, checklist=[
        ChecklistItem("mul(a,b) returns product", True,
                      "calc.py:4 defines mul returning a*b")])
    reviewer = FakeReviewer(passing)
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    # Pin intake: the claim's per-criterion COVERAGE check compares against
    # task.acceptance_criteria, and live enrichment would nondeterministically
    # expand it (and burn a real utility call) mid-test.
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None                       # nothing to merge
    assert "ALREADY-SATISFIED" in (outcome.report or "")
    assert reviewer.calls and reviewer.calls[-1]["mode"] == "already_satisfied"
    assert "calc.py:4" in reviewer.calls[-1]["claim_report"]
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "succeeded"
    assert attempts[-1]["review_passed"] == 1
    refreshed = await store.find_task(t.id)
    assert "ALREADY-SATISFIED" in (refreshed.context or {}).get(
        "already_satisfied_report", "")


async def test_refuted_already_satisfied_claim_fails_the_attempt(
    bare_repo, tmp_path, store
):
    """A lazy 'already done' dies on citation verification: the FAIL feeds the
    bounded loop as review findings and never reaches the human gate."""
    failing = ReviewDecision(passed=False, checklist=[
        ChecklistItem("mul(a,b) returns product", False,
                      "calc.py has no mul() at all — the citation is fiction",
                      severity="high")])
    reviewer = FakeReviewer(failing)
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    # Pin intake: the claim's per-criterion COVERAGE check compares against
    # task.acceptance_criteria, and live enrichment would nondeterministically
    # expand it (and burn a real utility call) mid-test.
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Tightened per PR #101 review: "not awaiting_approval" also passed for
    # DONE — the refuted claim must end on the failure side of the machine.
    assert outcome.status in (TaskStatus.FAILED, TaskStatus.ESCALATED)
    attempts = await store.list_attempts(t.id)
    assert any("refuted" in (a.get("failure_reason") or "") for a in attempts), \
        [(a.get("status"), a.get("failure_reason")) for a in attempts]


async def test_already_satisfied_with_no_reviewer_fails_closed(
    bare_repo, tmp_path, store
):
    """PR #101 review MEDIUM: the fail-closed branches were untested. No
    reviewer + no allow_advisory → the claim must NEVER pass unreviewed."""
    cfg = _config(tmp_path)
    # _config defaults allow_advisory=True for the fake-backend suite; this
    # test exercises the PRODUCTION fail-closed default.
    cfg.data["reviewer"]["allow_advisory"] = False
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=None)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    # Pin intake: the claim's per-criterion COVERAGE check compares against
    # task.acceptance_criteria, and live enrichment would nondeterministically
    # expand it (and burn a real utility call) mid-test.
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    refreshed = await store.find_task(t.id)
    assert refreshed.status is not TaskStatus.AWAITING_APPROVAL


async def test_already_satisfied_advisory_mode_is_honest_about_no_review(
    bare_repo, tmp_path, store
):
    """allow_advisory passes the claim to the human gate but must SAY the
    claim was not verified (PR #101 review LOW: false 'review verified' detail)."""
    cfg = _config(tmp_path)
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=None)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    # Pin intake: the claim's per-criterion COVERAGE check compares against
    # task.acceptance_criteria, and live enrichment would nondeterministically
    # expand it (and burn a real utility call) mid-test.
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert "NOT verified" in outcome.detail
    assert "verified every cited criterion" not in outcome.detail


class _UnavailableReviewer:
    async def review(self, task, **kwargs):
        from no_human.review.reviewer import _carry_usage, ReviewerUnavailable
        # Carrying spend exactly as `_agent_review` does: the rounds that
        # reached no verdict were still billed, and this exception is the only
        # thing that leaves the reviewer on this path.
        raise _carry_usage(
            ReviewerUnavailable("no verdict after retries"),
            [_SimpleNamespace(tokens_used=120_000, cache_read_tokens=900_000,
                             cache_creation_tokens=30_000, output_tokens=9_000)],
        )


async def test_already_satisfied_reviewer_unavailable_escalates(
    bare_repo, tmp_path, store
):
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=_UnavailableReviewer())
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    # Pin intake: the claim's per-criterion COVERAGE check compares against
    # task.acceptance_criteria, and live enrichment would nondeterministically
    # expand it (and burn a real utility call) mid-test.
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED
    # The gate escalated, but it had already PAID for its rounds. That handler
    # returns before the normal recording, so without its own
    # `_record_review_usage` call the whole gate's burn is missing from the
    # attempt row — and from the lifetime-budget ceiling that reads it.
    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt row to carry the reviewer's burn"
    assert attempts[-1]["review_tokens_used"] == 120_000
    assert attempts[-1]["review_cache_read_tokens"] == 900_000
    assert attempts[-1]["review_output_tokens"] == 9_000


class _CrashingReviewer:
    async def review(self, task, **kwargs):
        raise RuntimeError("reviewer exploded")


async def test_already_satisfied_reviewer_crash_fails_the_attempt(
    bare_repo, tmp_path, store
):
    """A crashed reviewer is a failing decision (fail closed), never a pass."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=_CrashingReviewer())
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    # Pin intake: the claim's per-criterion COVERAGE check compares against
    # task.acceptance_criteria, and live enrichment would nondeterministically
    # expand it (and burn a real utility call) mid-test.
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status in (TaskStatus.FAILED, TaskStatus.ESCALATED)
    attempts = await store.list_attempts(t.id)
    assert any("crashed" in (a.get("failure_reason") or "") for a in attempts)


# --------------------------------------------------------------------------- #
# f27f3b73: a review-only recovery round must stamp the sha it reviewed        #
# --------------------------------------------------------------------------- #

def _commit_real_work(work_dir):
    """Simulate attempt 1: real, reviewable work already on the branch."""
    (work_dir / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def mul(a, b):\n    return a * b\n"
    )
    (work_dir / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


_ALREADY_SATISFIED_CLAIM = (
    "Verified every criterion against the existing code.\n"
    "ALREADY-SATISFIED\n"
    "CRITERION: mul(a,b) returns product — MET — evidence: calc.py:4\n"
)


async def test_review_only_recovery_round_stamps_the_reviewed_sha(
    bare_repo, tmp_path, store
):
    """f27f3b73: attempt 1 commits real work and its reviewer session dies
    twice, unreviewed (TRANSIENT_INFRA park). On wake, a review-only recovery
    round (`_gate_already_satisfied`'s PASS path — reached via a "wake"-type
    resume, which `_already_satisfied_eligible` does not restrict the way it
    restricts `orphan_recovery`) re-reviews the already-committed diff and
    PASSES. That PASS must stamp the sha it reviewed into `review_history` —
    the only place `nh approve`'s merge precondition (`_review_pass_evidence`
    / `Orchestrator._rounds_for_head`) looks — or the task strands at an
    unmergeable awaiting_approval, exactly as it did live."""
    branch = "no-human/task-f27f3b73"
    _git(bare_repo, "checkout", "-b", branch)
    _commit_real_work(bare_repo)
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "add mul()")
    _git(bare_repo, "push", "-u", "origin", branch)

    repo = GitRepo(bare_repo)
    reviewed_sha = repo.head_sha()

    cfg = _config(tmp_path)
    passing = ReviewDecision(passed=True, checklist=[
        ChecklistItem("mul(a,b) returns product", True,
                      "calc.py:4 defines mul returning a*b")])
    reviewer = FakeReviewer(passing)
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    # The provenance a timed wake-resume writes — not a human, not an
    # orphan-recovery mid-phase restart. This is the exact resume shape
    # `_already_satisfied_eligible` does NOT restrict (only `orphan_recovery`
    # is gated), which is how the live incident reached this code path.
    t.context = {"eval_result": {"verdict": "accept"},
                "resume_from": {"by": "wake"}}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 2)

    outcome = await orch._gate_already_satisfied(
        t, repo, attempt_id, _ALREADY_SATISFIED_CLAIM, branch=branch,
        attempt_n=2,
    )

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    refreshed = await store.find_task(t.id)
    history = (refreshed.context or {}).get("review_history") or []
    assert history, "the PASS recorded nothing in review_history"
    assert history[-1]["sha"] == reviewed_sha
    assert history[-1]["passed"] is True

    # The merge precondition itself — unchanged, and now satisfied.
    head_sha = repo.head_sha()
    passed, evidence = _review_pass_evidence(refreshed.context or {}, head_sha, repo)
    assert passed, evidence


async def test_review_only_recovery_stamp_does_not_cover_a_later_rewrite(
    bare_repo, tmp_path, store
):
    """Control: after the review-only round stamps sha X, the branch is
    REWRITTEN (amended, not merely advanced) so X is no longer an ancestor of
    the new head. The merge precondition must still refuse — the stamp covers
    the exact commit reviewed, not whatever the branch points to later."""
    branch = "no-human/task-f27f3b73-moved"
    _git(bare_repo, "checkout", "-b", branch)
    _commit_real_work(bare_repo)
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "add mul()")

    repo = GitRepo(bare_repo)

    cfg = _config(tmp_path)
    passing = ReviewDecision(passed=True, checklist=[
        ChecklistItem("mul(a,b) returns product", True,
                      "calc.py:4 defines mul returning a*b")])
    reviewer = FakeReviewer(passing)
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"},
                "resume_from": {"by": "wake"}}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 2)

    await orch._gate_already_satisfied(
        t, repo, attempt_id, _ALREADY_SATISFIED_CLAIM, branch=branch,
        attempt_n=2,
    )

    # The branch is REWRITTEN, not advanced: the reviewed commit is no
    # longer reachable from the new head at all.
    _git(bare_repo, "commit", "--amend", "-m", "add mul() (reworded)")
    new_head = repo.head_sha()

    refreshed = await store.find_task(t.id)
    passed, evidence = _review_pass_evidence(refreshed.context or {}, new_head, repo)
    assert not passed, evidence
    assert "no review round is stamped" in evidence


async def test_review_only_recovery_round_fails_closed_on_unresolvable_head(
    bare_repo, tmp_path, store, monkeypatch
):
    """An unresolvable already-satisfied subject is refused without a stamp."""
    branch = "no-human/task-f27f3b73-unresolvable"
    _git(bare_repo, "checkout", "-b", branch)
    _commit_real_work(bare_repo)
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "add mul()")

    repo = GitRepo(bare_repo)
    monkeypatch.setattr(
        GitRepo, "head_sha",
        lambda self: (_ for _ in ()).throw(RuntimeError("git rev-parse failed")),
    )

    cfg = _config(tmp_path)
    passing = ReviewDecision(passed=True, checklist=[
        ChecklistItem("mul(a,b) returns product", True,
                      "calc.py:4 defines mul returning a*b")])
    reviewer = FakeReviewer(passing)
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"},
                "resume_from": {"by": "wake"}}
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 2)

    outcome = await orch._gate_already_satisfied(
        t, repo, attempt_id, _ALREADY_SATISFIED_CLAIM, branch=branch,
        attempt_n=2,
    )

    assert outcome.status is TaskStatus.FAILED
    refreshed = await store.find_task(t.id)
    history = (refreshed.context or {}).get("review_history") or []
    assert not history, "an unresolvable head must not fabricate a review stamp"
    monkeypatch.undo()
    real_head = repo.head_sha()
    passed, evidence = _review_pass_evidence(refreshed.context or {}, real_head, repo)
    assert not passed, evidence
    assert "no review round is stamped" in evidence


# --------------------------------------------------------------------------- #
# The one reformat nudge: a right answer must not die on phrasing              #
# --------------------------------------------------------------------------- #

class WrongShapeBackend:
    """Zero edits, a CORRECT report — in the wrong shape.

    The measured defect (bench, 2026-08-03): the agent answers the question and
    writes "## Answer …" with real evidence, `_parse_already_satisfied` returns
    None, and the attempt dies on formatting. `restated` is what it says when
    asked (on its own session, via ``resume``) to restate it in the contract.
    """

    REPORT = ("## Answer\n\n"
              "mul(a, b) already exists and returns the product — see calc.py:4. "
              "Nothing to change.")
    CONTRACT = ("ALREADY-SATISFIED\n"
                "CRITERION: mul(a,b) returns product — MET — evidence: calc.py:4\n")
    capabilities = RESUMABLE

    def __init__(self, restated: str = CONTRACT, first: str = REPORT):
        self.calls: list[dict] = []
        self._restated = restated
        self._first = first

    @property
    def prompts(self) -> list[str]:
        return [c["prompt"] for c in self.calls]

    @property
    def nudges(self) -> list[dict]:
        return [c for c in self.calls if _REFORMAT_NUDGE_MARKER in c["prompt"]]

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls.append({"prompt": prompt, "resume": resume,
                           "max_turns": max_turns, "effort": effort})
        if resume is not None:
            return AgentResult(final_text=self._restated, num_turns=1,
                               is_error=False, tokens_used=40, session_id="s",
                               stop_reason="end_turn")
        return AgentResult(final_text=self._first, num_turns=5, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


async def test_wrong_shaped_zero_diff_report_gets_exactly_one_reformat_nudge(
    bare_repo, tmp_path, store
):
    """A correct answer in the wrong format buys ONE cheap single-turn restate
    on the SAME session — and, once it parses, goes to the same reviewer gate a
    first-try parse would have reached."""
    passing = ReviewDecision(passed=True, checklist=[
        ChecklistItem("mul(a,b) returns product", True,
                      "calc.py:4 defines mul returning a*b")])
    reviewer = FakeReviewer(passing)
    cfg = _config(tmp_path)
    backend = WrongShapeBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    # Pin intake, as every test in this section does: live enrichment would
    # expand the criteria the claim's coverage check counts against.
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Exactly one nudge, carrying the contract text verbatim.
    assert len(backend.nudges) == 1, backend.prompts
    nudge = backend.nudges[0]
    assert nudge["prompt"] == _REFORMAT_NUDGE
    assert "ALREADY-SATISFIED" in nudge["prompt"]
    assert "do not edit files" in nudge["prompt"]
    # Cheap, and on the agent's OWN session — not a fresh one.
    assert nudge["resume"] == "s"
    assert nudge["max_turns"] == 1
    assert nudge["effort"] == "low"
    # The reviewer gate is reached exactly as a first-try parse reaches it.
    assert reviewer.calls and reviewer.calls[-1]["mode"] == "already_satisfied"
    assert "calc.py:4" in reviewer.calls[-1]["claim_report"]
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert "ALREADY-SATISFIED" in (outcome.report or "")
    # One attempt: the nudge rescued it in place, it did not buy a retry.
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 1
    assert attempts[-1]["status"] == "succeeded"
    # The nudge's spend bills where the coder's turns bill — summed onto the
    # attempt row, never dropped and never written over the coder's own.
    assert attempts[-1]["tokens_used"] == 140
    assert attempts[-1]["turns_used"] == 6


async def test_a_nudge_that_still_does_not_parse_fails_the_attempt_unchanged(
    bare_repo, tmp_path, store
):
    """The rescue is one turn, not a negotiation: a reply that still misses the
    contract fails on the SAME detail string as before the nudge existed, and
    the escalation still carries the agent's ORIGINAL words."""
    from no_human.core.orchestrator import _NO_CHANGES_DETAIL

    cfg = _config(tmp_path)
    backend = WrongShapeBackend(restated="Still just prose, I'm afraid.")
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt was recorded"
    assert all(a["failure_reason"] == _NO_CHANGES_DETAIL for a in attempts), \
        [a["failure_reason"] for a in attempts]
    # ONE nudge per attempt — never two for the same zero-diff completion.
    assert len(backend.nudges) == len(attempts), backend.prompts
    # zero_diff_reason keeps the ORIGINAL report, not the nudge's restatement:
    # it answers "what did the agent conclude", and the restatement is an echo
    # of a formatting instruction we wrote.
    refreshed = await store.find_task(t.id)
    stated = (refreshed.context or {}).get("zero_diff_reason") or ""
    assert stated == WrongShapeBackend.REPORT
    assert "Still just prose" not in stated


async def test_an_empty_final_report_is_never_nudged(bare_repo, tmp_path, store):
    """Anti-fabrication, unchanged: with nothing to restate, asking for the
    contract would be asking the agent to AUTHOR a claim. Immediate fail."""
    from no_human.core.orchestrator import _NO_CHANGES_DETAIL

    cfg = _config(tmp_path)
    backend = WrongShapeBackend(first="   \n  ")
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    await orch.run_task(t)

    assert backend.nudges == [], backend.prompts
    assert all(c["resume"] is None for c in backend.calls)
    attempts = await store.list_attempts(t.id)
    assert attempts and all(
        a["failure_reason"] == _NO_CHANGES_DETAIL for a in attempts)


async def test_only_one_nudge_per_attempt_even_on_a_second_zero_diff(
    bare_repo, tmp_path, store
):
    """The budget is per ATTEMPT, not per zero-diff completion: a second
    non-parsing completion inside the same attempt buys no second turn."""
    from no_human.vcs import GitRepo

    cfg = _config(tmp_path)
    backend = WrongShapeBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)
    attempt_id = await store.create_attempt(t.id, 1)
    result = AgentResult(
        final_text=WrongShapeBackend.REPORT, num_turns=5, is_error=False,
        tokens_used=100, session_id="s", stop_reason="end_turn")
    # A REAL repo: the nudge snapshots and restores the worktree around the
    # turn, so a stand-in with only a `.path` would not exercise it.
    repo = GitRepo(bare_repo)

    first = await orch._reformat_nudge(
        t, result, repo=repo, attempt_id=attempt_id)
    second = await orch._reformat_nudge(
        t, result, repo=repo, attempt_id=attempt_id)

    assert first is not None and "ALREADY-SATISFIED" in first
    assert second is None
    assert len(backend.nudges) == 1, backend.prompts
    # A DIFFERENT attempt gets its own one.
    other = await store.create_attempt(t.id, 2)
    assert await orch._reformat_nudge(
        t, result, repo=repo, attempt_id=other) is not None
    assert len(backend.nudges) == 2


async def test_no_nudge_without_a_session_to_continue(bare_repo, tmp_path, store):
    """The nudge is a CONTINUATION — it asks the agent to restate what it just
    wrote. Three ways that is not available, and all three must yield NO nudge
    rather than a fresh-context session being asked to restate a report it
    never wrote. The third is the fail-closed default: a backend that says
    NOTHING about resuming is treated as unable to, never assumed able."""
    from no_human.vcs import GitRepo

    cfg = _config(tmp_path)
    backend = WrongShapeBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)
    repo = GitRepo(bare_repo)

    sessionless = AgentResult(
        final_text=WrongShapeBackend.REPORT, num_turns=5, is_error=False,
        tokens_used=100, session_id=None, stop_reason="end_turn")
    assert await orch._reformat_nudge(
        t, sessionless, repo=repo,
        attempt_id=await store.create_attempt(t.id, 1)) is None

    with_session = AgentResult(
        final_text=WrongShapeBackend.REPORT, num_turns=5, is_error=False,
        tokens_used=100, session_id="s", stop_reason="end_turn")
    backend.capabilities = _SimpleNamespace(session_resume=False)
    assert await orch._reformat_nudge(
        t, with_session, repo=repo,
        attempt_id=await store.create_attempt(t.id, 2)) is None

    # SILENT on the question — the default must be NO, not yes.
    backend.capabilities = _SimpleNamespace(name="fake")
    assert not hasattr(backend.capabilities, "session_resume")
    assert await orch._reformat_nudge(
        t, with_session, repo=repo,
        attempt_id=await store.create_attempt(t.id, 3)) is None

    assert backend.calls == []


class SneakyNudgeBackend(WrongShapeBackend):
    """The nudge turn IGNORES "do not edit files" and writes one.

    Not a hypothetical: a review probe drove exactly this and watched the stray
    file get committed by the NEXT attempt and opened in a PR, because the
    nudge runs after `has_changes()` has already been read.

    ``stage`` decides whether it also `git add`s what it wrote. That is not a
    detail: a staged new file reads ``A `` rather than ``??``, which is how the
    first fix for this missed it — and `git checkout -- <p>` is a silent no-op
    on a staged addition, so the file survived and shipped while the advisory
    claimed it had been reverted.
    """

    STRAY = "sneaky.py"

    def __init__(self, restated=WrongShapeBackend.CONTRACT, *, stage=False,
                 mutate_tracked=False):
        super().__init__(restated=restated)
        self._stage = stage
        self._mutate_tracked = mutate_tracked
        #: What each CODER call found when it STARTED — the only place the
        #: invariant can be observed honestly. Asserting on `bare_repo` after
        #: `run_task` returns proves nothing twice over: the agent works in an
        #: isolated worktree (so the operator's checkout was never touched),
        #: and that worktree is torn down in `run_task`'s `finally` (so a
        #: missing file is "the directory is gone", not "the revert worked").
        #: A first draft of this test asserted exactly that and passed against
        #: BOTH broken classification rules.
        self.tree_seen: list[dict] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        if resume is None:
            from no_human.vcs import GitRepo as _GitRepo
            self.tree_seen.append({
                "stray": (Path(cwd) / self.STRAY).exists(),
                "calc": (Path(cwd) / "calc.py").read_text()
                        if (Path(cwd) / "calc.py").exists() else None,
                # The COMMITTABLE view — `has_changes()`/`stage_all`'s own
                # exclusions. The raw porcelain is not it: the orchestrator
                # stages its copied skills and instructions under `.claude/`,
                # so a raw read is never empty in normal operation and an
                # assertion on it would fail on healthy runs.
                "dirty": _git_out(cwd, "status", "--porcelain",
                                  "--untracked-files=all", "--", ".",
                                  *_GitRepo._EPHEMERAL),
            })
        else:
            (Path(cwd) / self.STRAY).write_text("# no coder wrote this\n")
            if self._mutate_tracked:
                (Path(cwd) / "calc.py").write_text("# clobbered by the nudge\n")
            if self._stage:
                _git(cwd, "add", "-A")
        return await super().run(
            prompt, cwd=cwd, max_turns=max_turns, effort=effort, resume=resume,
            on_event=on_event, supervisor_hook=supervisor_hook, **kwargs)


@pytest.mark.parametrize("stage", [False, True], ids=["unstaged", "staged"])
async def test_a_nudge_that_writes_files_is_reverted_before_the_next_attempt(
    bare_repo, tmp_path, store, stage
):
    """🔴 The regression the review proved. The nudge is a WRITE-CAPABLE turn
    running after `has_changes()` was evaluated, so a file it drops is invisible
    to this attempt and gets committed by the next one — a PR carrying a file no
    coder produced, and a deleted two-zero-diff escalation because attempt 2 no
    longer looked unproductive. The tree must be restored before the nudge
    returns.

    Both parametrisations matter and the SECOND is the one that regressed: a
    nudge that stages what it writes reads ``A `` instead of ``??``, and the
    first fix routed that to a restore-from-index that does nothing at all.
    """
    from no_human.core.orchestrator import _NO_CHANGES_DETAIL

    cfg = _config(tmp_path)
    backend = SneakyNudgeBackend(
        restated="Still prose. Also I wrote a file.", stage=stage)
    events: list = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # The nudge DID write (otherwise this test proves nothing about reverting).
    assert len(backend.nudges) >= 1, backend.prompts
    # …and every write was reverted, with an advisory naming the path.
    reverts = [e for e in events if e.get("kind") == "advisory"
               and SneakyNudgeBackend.STRAY in (e.get("text") or "")]
    assert reverts, [e for e in events if e.get("kind") == "advisory"]
    # The advisory must be TRUE, not merely present: "reverted" is a claim
    # about the tree, and the staged case is exactly where it used to be a lie.
    assert "reverted" in (reverts[0].get("text") or "")
    # Both attempts still fail as zero-diff — the stray file never made one of
    # them look productive.
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 2, attempts
    assert all(a["failure_reason"] == _NO_CHANGES_DETAIL for a in attempts), \
        [a["failure_reason"] for a in attempts]
    # …so the two-consecutive-zero-diff escalation still fires, and no PR opened.
    assert outcome.status is TaskStatus.ESCALATED, outcome
    assert outcome.pr_url is None
    # 🔴 THE INVARIANT, observed where it lives: what attempt 2's coder found
    # when it started. Not a post-hoc look at `bare_repo` — the agent works in
    # an isolated worktree that is deleted afterwards, so that look is vacuous.
    assert len(backend.tree_seen) == 2, backend.tree_seen
    second = backend.tree_seen[1]
    assert second["stray"] is False, "the stray file survived into attempt 2"
    assert second["dirty"] == "", second["dirty"]


async def test_the_revert_restores_a_tracked_file_instead_of_deleting_it(
    bare_repo, tmp_path, store
):
    """The revert must not become its own data loss. A nudge that STAGES a
    change to an existing tracked file is, like a staged new file, absent from
    the before-snapshot — so a revert keyed on "absent ⇒ created" would
    `rm --cached` + unlink a real source file. Measured before it was written:
    the probe left a staged deletion and no file on disk.

    The rule is existence at HEAD, so this path is restored to its committed
    content and the stray beside it is removed."""
    cfg = _config(tmp_path)
    backend = SneakyNudgeBackend(restated="prose", stage=True,
                                 mutate_tracked=True)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)
    original = (Path(bare_repo) / "calc.py").read_text()

    await orch.run_task(t)

    assert len(backend.nudges) >= 1
    # Again read from what attempt 2 SAW, not from the operator's checkout —
    # which the agent never touches, and which therefore "passes" no matter
    # how badly the revert behaves.
    assert len(backend.tree_seen) == 2, backend.tree_seen
    second = backend.tree_seen[1]
    assert second["calc"] is not None, "the revert DELETED a tracked source file"
    assert second["calc"] == original, second["calc"]
    assert second["stray"] is False
    assert second["dirty"] == "", second["dirty"]


class CommittingNudgeBackend(WrongShapeBackend):
    """The nudge turn commits. A commit leaves a CLEAN status, so the
    snapshot/revert cannot see it at all — the one shape that needs HEAD."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        if resume is not None:
            (Path(cwd) / "committed_by_nudge.py").write_text("# not the coder\n")
            _git(cwd, "add", "-A")
            _git(cwd, "commit", "-m", "nudge did this")
        return await super().run(
            prompt, cwd=cwd, max_turns=max_turns, effort=effort, resume=resume,
            on_event=on_event, supervisor_hook=supervisor_hook, **kwargs)


async def test_a_nudge_that_commits_is_advertised_and_its_claim_discarded(
    bare_repo, tmp_path, store
):
    """A committing nudge is invisible to a status-based revert, so HEAD is
    snapshotted too. The claim it returns is thrown away even though it PARSES
    — a report from a turn that just rewrote the branch is not evidence — and
    the attempt fails as the zero diff it was."""
    from no_human.core.orchestrator import _NO_CHANGES_DETAIL

    cfg = _config(tmp_path)
    # The restatement is a PERFECTLY VALID claim: this test is about the commit
    # being disqualifying on its own, not about the format.
    backend = CommittingNudgeBackend()
    events: list = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    await orch.run_task(t)

    assert len(backend.nudges) >= 1
    committed = [e for e in events if e.get("kind") == "advisory"
                 and "COMMITTED" in (e.get("text") or "")]
    assert committed, [e for e in events if e.get("kind") == "advisory"]
    attempts = await store.list_attempts(t.id)
    assert all(a["failure_reason"] == _NO_CHANGES_DETAIL for a in attempts), \
        [a["failure_reason"] for a in attempts]
    # The parsing claim never reached the reviewer gate.
    refreshed = await store.find_task(t.id)
    assert "already_satisfied_report" not in (refreshed.context or {})


#: What the aborting nudge below feeds before it is stopped. Named because the
#: assertions are arithmetic on it: an abort must bill the coder turn's own
#: total PLUS this, and billing only the coder's total is the bug that hides
#: here (it passes any "> 0" assertion).
NUDGE_FED_TOKENS = 7


class _AbortingNudgeBackend(WrongShapeBackend):
    """The nudge turn feeds some tokens, then raises one of the sink's three
    controls — the way a real turn does when a pause / budget cross / doom-loop
    is seen partway through."""

    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        if resume is not None:
            self.calls.append({"prompt": prompt, "resume": resume,
                               "max_turns": max_turns, "effort": effort})
            # Spend REACHES the sink before the abort, exactly as a real
            # partial turn's does — otherwise "the spend was billed" is a
            # claim about zero tokens.
            if on_event is not None:
                on_event(AgentEvent(kind="usage", meta={
                    "tokens_used": NUDGE_FED_TOKENS}))
            raise self._exc
        return await super().run(
            prompt, cwd=cwd, max_turns=max_turns, effort=effort, resume=resume,
            on_event=on_event, supervisor_hook=supervisor_hook, **kwargs)


async def test_a_budget_abort_in_the_nudge_still_parks_and_still_bills(
    bare_repo, tmp_path, store
):
    """Swallowing the sink's controls lost three things at once: the reason,
    the BUDGET_EXHAUSTED routing, and the tokens the turn had already fed. The
    lifetime cap is set below what the attempt spends, so the cross must PARK,
    not just fail the attempt."""
    from no_human.core.orchestrator import BudgetAbort

    cfg = _config(tmp_path)
    # A fresh task's first attempt has no measured startup history, so the
    # loop-head startup floor (`_check_attempt_startup_floor`) would
    # otherwise fall back to the config default (250,000) and refuse to
    # start the attempt at all against this fixture's tiny lifetime cap —
    # before the mid-attempt abort path this test exercises ever runs.
    # Zeroed to neutralize a gate this test isn't exercising.
    cfg.data.setdefault("bounds", {})["min_viable_attempt_weighted_tokens"] = 0
    backend = _AbortingNudgeBackend(BudgetAbort("attempt spend crossed the cap"))
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    # Chosen to sit BETWEEN the two numbers, so the test can tell them apart:
    # above what the nudge feeds (or the sink would abort on its own message
    # before the backend does) and below the attempt's total spend (or the
    # lifetime read after the abort would not cross and nothing would park).
    # `budget_unit: weighted` marks the number as already being in the current
    # unit — an unmarked cap is migrated, which would silently multiply it.
    t.config = {"lifetime_tokens": 50, "budget_unit": "weighted"}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    # ONE attempt. The park is THIS handler's routing decision, not a later
    # loop-head budget check — which is the whole difference between "the
    # controls were re-raised" and "they were swallowed and something else
    # noticed eventually".
    assert len(attempts) == 1, [a["failure_reason"] for a in attempts]
    assert attempts[-1]["status"] == "failed"
    assert "budget-abort" in (attempts[-1]["failure_reason"] or ""), attempts[-1]
    # The reason survived — not "agent produced no file changes".
    assert "crossed the cap" in (attempts[-1]["failure_reason"] or "")
    # The NUDGE's spend reached the ledger, not just the coder turn's. `> 0`
    # would pass on the coder's 100 alone and prove nothing about the delta,
    # which is the number that used to be dropped with the exception.
    coder_tokens = 100                      # WrongShapeBackend's own result
    assert attempts[-1]["tokens_used"] == coder_tokens + NUDGE_FED_TOKENS, \
        attempts[-1]["tokens_used"]
    # And the lifetime cross parked instead of quietly failing an attempt.
    fresh = await store.find_task(t.id)
    assert (fresh.blocker or {}).get("category") == "BUDGET_EXHAUSTED", \
        fresh.blocker
    assert outcome.status in (TaskStatus.BLOCKED, TaskStatus.ESCALATED,
                              TaskStatus.FAILED), outcome
    # Since `budget.exhaustion_terminal` (default on) the cross ENDS the task
    # rather than asking a human whose answer was always "stop". Two things
    # this test already proves, now stated: it came off the blocker funnel
    # (`off_ramp`) rather than being a plain attempt failure, and `len(attempts)
    # == 1` above is the evidence the bounded loop did NOT read that FAILED as
    # a retryable attempt and raise the same blocker a second time.
    assert outcome.off_ramp is True, outcome
    assert (fresh.blocker or {}).get("question") is None, fresh.blocker


class _MustNotRunBackend(WrongShapeBackend):
    """A backend that asserts if the coder path ever reaches it — proves a
    "don't start" gate for real, the way
    ``test_infra_not_work.py::test_drive_honours_a_pending_stop_before_the_human_gated_route``
    proves ``_resume_human_gated`` is skipped: by making the wrong path
    explode, not by re-deriving "was it skipped" from a hand-simulated emit.
    """

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        raise AssertionError(
            "the backend ran — the loop started an attempt the remaining "
            "lifetime budget could not afford")


async def test_the_loop_head_refuses_to_start_an_attempt_it_cannot_afford(
    bare_repo, tmp_path, store
):
    """AC1, loop-head half (INCIDENT 2026-08-20, run 123dea00): the unit test
    in test_lifetime_budget.py proves `_check_attempt_startup_floor`'s own
    verdict; this proves the LOOP honours it — that `_drive` never reaches
    `attempt_start` / the backend when the floor refuses, which is the actual
    defect (a dead attempt was STARTED, not merely computable as unaffordable).

    RED on pre-fix code (no startup-floor gate at all): the loop starts the
    attempt regardless of how little budget remains, `_MustNotRunBackend.run`
    is reached and raises, and the `attempt_start` / attempt-count assertions
    below fail.
    """
    cfg = _config(tmp_path)
    backend = _MustNotRunBackend()
    events: list = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    # No prior attempt exists, so `_min_viable_attempt_cost` has no measured
    # history and falls back to the config default
    # (`bounds.min_viable_attempt_weighted_tokens`, 250,000 — see bounds.py).
    # A cap this tight is UNDER both lifetime caps (nothing has been spent
    # yet, so `_check_lifetime_budget` returns None) but leaves a remaining
    # budget the startup floor refuses outright.
    t.config = {"lifetime_tokens": 100, "budget_unit": "weighted"}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert backend.calls == [], (
        "the backend was invoked despite an unaffordable startup floor")
    assert not any(e.get("kind") == "attempt_start" for e in events), events
    assert await store.list_attempts(t.id) == []
    fresh = await store.find_task(t.id)
    assert (fresh.blocker or {}).get("category") == "BUDGET_EXHAUSTED", \
        fresh.blocker
    assert "floor" in (fresh.blocker or {}).get("root_cause_hypothesis", "")
    assert outcome.off_ramp is True, outcome


async def test_a_stuck_abort_in_the_nudge_fails_the_attempt_with_its_reason(
    bare_repo, tmp_path, store
):
    """StuckAbort keeps its own semantics: a FAILED attempt carrying the
    doom-loop reason, so the bounded loop retries with fresh context — never a
    zero-diff failure wearing the wrong label."""
    from no_human.core.orchestrator import StuckAbort

    cfg = _config(tmp_path)
    backend = _AbortingNudgeBackend(StuckAbort("identical tool call x3"))
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert attempts[0]["status"] == "failed"
    assert "stuck-abort: identical tool call x3" == attempts[0]["failure_reason"]
    # Coder turn + the partial the nudge fed before it was stopped.
    assert attempts[0]["tokens_used"] == 100 + NUDGE_FED_TOKENS, attempts[0]


async def test_an_unexpected_nudge_error_falls_back_to_the_zero_diff_failure(
    bare_repo, tmp_path, store
):
    """Everything that is NOT one of the three controls stays swallowed: the
    rescue is best-effort, so a transport error degrades to 'no claim' and the
    attempt fails exactly as it did before the nudge existed."""
    from no_human.core.orchestrator import _NO_CHANGES_DETAIL

    cfg = _config(tmp_path)
    backend = _AbortingNudgeBackend(RuntimeError("transport died"))
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert attempts and all(
        a["failure_reason"] == _NO_CHANGES_DETAIL for a in attempts), \
        [a["failure_reason"] for a in attempts]
    refreshed = await store.find_task(t.id)
    assert (refreshed.context or {}).get(
        "zero_diff_reason") == WrongShapeBackend.REPORT


# --------------------------------------------------------------------------- #
# B3: the suite ran twice per happy path                                       #
# --------------------------------------------------------------------------- #

def _count_test_runs(monkeypatch):
    """Count real `runner.run_tests` invocations, keeping its behavior."""
    from no_human.testing import runner as _runner
    calls: list[str] = []
    real = _runner.run_tests

    def counting(repo_path, test_cmd=None, *a, **kw):
        calls.append(str(test_cmd))
        return real(repo_path, test_cmd, *a, **kw)

    monkeypatch.setattr("no_human.core.orchestrator.runner.run_tests", counting)
    return calls


async def test_the_suite_runs_once_per_attempt_not_twice(
    bare_repo, tmp_path, store, monkeypatch
):
    """`_run_review` runs the suite for the reviewer's evidence, then TESTING ran
    the identical command against the identical commit. One run, two consumers.

    A reviewer must be wired: without one `_run_review` returns an advisory pass
    before ever running tests, so the duplicate never appears."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    calls = _count_test_runs(monkeypatch)
    cfg = _config(tmp_path)
    events: list[dict] = []
    reviewer = FakeReviewer(ReviewDecision(
        passed=True,
        checklist=[ChecklistItem("mul implemented", True, "calc.py:4 returns a*b")],
    ))
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append, reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert len(calls) == 1, f"suite ran {len(calls)}× in one attempt"
    reuse = [e for e in events if e.get("kind") == "tests" and e.get("cached")]
    assert len(reuse) == 1
    assert "reused the reviewer's run" in reuse[0]["text"]


async def test_a_dirty_tree_never_reuses_a_cached_pass(bare_repo, tmp_path, store):
    """A cached result feeding the review gate would be a false pass. If the tree
    moved under us, re-run — correctness outranks the saved subprocess."""
    from no_human.vcs import GitRepo

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    repo = GitRepo(bare_repo)

    first, cached = await orch._run_tests_once(repo, "true")
    assert cached is False
    _, cached = await orch._run_tests_once(repo, "true")
    assert cached is True, "a clean tree at the same commit should reuse"

    # Someone touched a tracked source file after the cached run.
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return 999\n")
    _, cached = await orch._run_tests_once(repo, "true")
    assert cached is False, "a dirty tree must force a fresh run"


async def test_a_different_command_is_not_a_cache_hit(bare_repo, tmp_path, store):
    """The layered TestPlan path runs different commands than the reviewer's."""
    from no_human.vcs import GitRepo

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    repo = GitRepo(bare_repo)

    await orch._run_tests_once(repo, "true")
    _, cached = await orch._run_tests_once(repo, "true -x")
    assert cached is False


# --------------------------------------------------------------------------- #
# SCRUM-45: layered TestPlan path parity — source_repo threading (SCRUM-35)    #
# and traceback_block aggregation into attempt_failed detail (SCRUM-40)        #
# --------------------------------------------------------------------------- #


async def test_layered_test_plan_threads_source_repo(bare_repo, tmp_path, store):
    """SCRUM-35 parity: the layered path must pass source_repo to run_test_plan
    (which threads it to every run_tests call), exactly like the single-command
    path's ``_run_tests_once`` does — otherwise a worktree layer's node command
    never gets the node_modules symlink."""
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan
    from no_human.testing.plan_runner import LayerResult, PlanResult
    from no_human.testing.runner import TestRunResult
    import no_human.testing.plan_runner as plan_runner_mod

    plan = TestPlan(layers=[
        TestLayer(name="unit", command="true", gating=Gating.BLOCKING),
    ])
    captured = {}

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        captured["source_repo"] = kwargs.get("source_repo")
        tr = TestRunResult(ran=True, ok=True, passed=1, failed=0, errors=0,
                            command="true", output="1 passed")
        lr = LayerResult(layer_name="unit", gating=Gating.BLOCKING, result=tr)
        return PlanResult(layer_results=[lr])

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    async def fake_resolve_test_plan(task):
        return plan

    with _patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         _patch.object(orch, "_primary_repo_path", lambda p: "/fake/primary"), \
         _patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert captured["source_repo"] == Path("/fake/primary")


async def test_layered_test_plan_source_repo_none_when_not_a_worktree(
    bare_repo, tmp_path, store,
):
    """When the task's repo is not a worktree (_primary_repo_path -> None),
    source_repo passed to run_test_plan is None — no phantom symlink target.

    Worktree isolation is on by default, so this mode has to be asked for
    explicitly (`isolation.enabled: false`) — that is the whole point of the
    path under test."""
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan
    from no_human.testing.plan_runner import LayerResult, PlanResult
    from no_human.testing.runner import TestRunResult
    import no_human.testing.plan_runner as plan_runner_mod

    plan = TestPlan(layers=[
        TestLayer(name="unit", command="true", gating=Gating.BLOCKING),
    ])
    captured = {}

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        captured["source_repo"] = kwargs.get("source_repo")
        tr = TestRunResult(ran=True, ok=True, passed=1, failed=0, errors=0,
                            command="true", output="1 passed")
        lr = LayerResult(layer_name="unit", gating=Gating.BLOCKING, result=tr)
        return PlanResult(layer_results=[lr])

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    cfg = _config(tmp_path)
    cfg.data["isolation"]["enabled"] = False   # the not-a-worktree mode
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None))
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    async def fake_resolve_test_plan(task):
        return plan

    with _patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         _patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        await orch.run_task(t)

    assert "source_repo" in captured
    assert captured["source_repo"] is None


async def test_layered_test_plan_failure_detail_keeps_only_the_root_cause(
    bare_repo, tmp_path, store,
):
    """D1.1 (2026-08-31, superseding SCRUM-40 parity), CORRECTED by review
    finding #6: a layered-plan failure appends only the FIRST failing
    BLOCKING layer's traceback_block to the attempt_failed detail — that is
    the layer whose failure actually explains `plan_result.ok=False`
    (`PlanResult.ok` is "no blocking layer failed"), and it is also what the
    adjacent stuck-detection lookup uses (one shared lookup now feeds both,
    so they can never name different layers). A LATER layer's excerpt
    (integration's, here — dependent on the same root cause) is dropped
    entirely, not merely reordered: keeping the downstream failure and
    dropping the root cause would bury the one traceback a human actually
    needs. Concatenating every layer's block used to build a multi-KB
    `failure_reason` on a multi-layer plan (SCRUM-40 parity)."""
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan
    from no_human.testing.plan_runner import LayerResult, PlanResult
    from no_human.testing.runner import TestRunResult
    import no_human.testing.plan_runner as plan_runner_mod

    plan = TestPlan(layers=[
        TestLayer(name="unit", command="pytest -q", gating=Gating.BLOCKING),
        TestLayer(name="integration", command="pytest -q", gating=Gating.BLOCKING,
                  depends_on=["unit"]),
    ])

    tr_unit = TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command="pytest -q", output="1 failed",
        traceback_excerpts={
            "test_unit.py::test_a": "AssertionError: Root Cause Here\n    assert 1 == 2",
        },
    )
    tr_integration = TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command="pytest -q", output="1 failed",
        traceback_excerpts={
            "test_integration.py::test_b": "ValueError: Downstream Context\n    raise ValueError",
        },
    )
    lr_unit = LayerResult(layer_name="unit", gating=Gating.BLOCKING, result=tr_unit)
    lr_integration = LayerResult(
        layer_name="integration", gating=Gating.BLOCKING, result=tr_integration,
    )

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        return PlanResult(layer_results=[lr_unit, lr_integration])

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    async def fake_resolve_test_plan(task):
        return plan

    with _patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         _patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        outcome = await orch.run_task(t)

    failed_events = [e for e in events if e.get("kind") == "attempt_failed"]
    assert failed_events, "expected an attempt_failed event"
    detail = failed_events[0]["text"]
    # Only the FIRST failing BLOCKING layer's (unit's) excerpt survives —
    # the root cause, not the downstream layer dependent on it.
    assert "AssertionError: Root Cause Here" in detail
    assert "ValueError: Downstream Context" not in detail, (
        "a LATER layer's traceback leaked into failure_reason — only the "
        "first failing BLOCKING layer's excerpt (the root cause) may appear")
    # outcome.detail is the max_attempts escalation wrapper around the same
    # attempt text — containment, not equality (the wrapper adds its prefix).
    assert detail in outcome.detail


async def test_layered_test_plan_failure_detail_caps_the_last_traceback(
    bare_repo, tmp_path, store,
):
    """D1.1: the surviving (last) layer's traceback_block is ALSO tail-capped
    to 1200 chars — a single layer's own excerpt can itself be large even
    after `runner._cap_excerpt`'s per-test cap, when several tests failed in
    the same layer."""
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan
    from no_human.testing.plan_runner import LayerResult, PlanResult
    from no_human.testing.runner import TestRunResult
    import no_human.testing.plan_runner as plan_runner_mod

    plan = TestPlan(layers=[
        TestLayer(name="unit", command="pytest -q", gating=Gating.BLOCKING),
    ])
    huge_traceback = "X" * 5000
    tr_unit = TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command="pytest -q", output="1 failed",
        traceback_excerpts={"test_unit.py::test_a": huge_traceback},
    )
    lr_unit = LayerResult(layer_name="unit", gating=Gating.BLOCKING, result=tr_unit)

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        return PlanResult(layer_results=[lr_unit])

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    async def fake_resolve_test_plan(task):
        return plan

    with _patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         _patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        await orch.run_task(t)

    failed_events = [e for e in events if e.get("kind") == "attempt_failed"]
    assert failed_events, "expected an attempt_failed event"
    detail = failed_events[0]["text"]
    assert huge_traceback not in detail, "the uncapped 5000-char excerpt reached failure_reason"
    assert "X" * 1200 in detail, "the last 1200 chars of the excerpt must survive"
    assert "X" * 1201 not in detail, "more than 1200 chars of the excerpt survived"


async def test_layered_failure_excerpt_skips_an_earlier_failing_advisory_layer(
    bare_repo, tmp_path, store,
):
    """D1.1 review finding #6: an ADVISORY layer can fail WITHOUT stopping
    the plan or flipping `plan_result.ok` (only a BLOCKING failure does
    that — see `PlanResult.ok`'s own docstring), so an advisory failure
    earlier in `layer_results` must never be mistaken for the layer that
    explains `plan_result.ok=False`. The excerpt must come from the
    BLOCKING layer's own traceback, not the advisory one that failed first."""
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan
    from no_human.testing.plan_runner import LayerResult, PlanResult
    from no_human.testing.runner import TestRunResult
    import no_human.testing.plan_runner as plan_runner_mod

    plan = TestPlan(layers=[
        TestLayer(name="lint", command="ruff check", gating=Gating.ADVISORY),
        TestLayer(name="unit", command="pytest -q", gating=Gating.BLOCKING,
                  depends_on=["lint"]),
    ])
    tr_lint = TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command="ruff check", output="1 failed",
        traceback_excerpts={"lint": "AdvisoryNoise: this never stopped the plan"},
    )
    tr_unit = TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command="pytest -q", output="1 failed",
        traceback_excerpts={
            "test_unit.py::test_a": "AssertionError: The Real Blocking Failure",
        },
    )
    lr_lint = LayerResult(layer_name="lint", gating=Gating.ADVISORY, result=tr_lint)
    lr_unit = LayerResult(layer_name="unit", gating=Gating.BLOCKING, result=tr_unit)

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        return PlanResult(layer_results=[lr_lint, lr_unit])

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    async def fake_resolve_test_plan(task):
        return plan

    with _patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         _patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan):
        await orch.run_task(t)

    failed_events = [e for e in events if e.get("kind") == "attempt_failed"]
    assert failed_events, "expected an attempt_failed event"
    detail = failed_events[0]["text"]
    assert "AssertionError: The Real Blocking Failure" in detail
    assert "AdvisoryNoise" not in detail, (
        "the earlier ADVISORY layer's failure was mistaken for the layer "
        "that explains plan_result.ok=False")


# --------------------------------------------------------------------------- #
# D21 / B4: context distillation belongs on the utility tier                   #
# --------------------------------------------------------------------------- #

class _Chunk:
    def __init__(self, content, source="file", title="big.py"):
        self.content, self.source, self.title = content, source, title


async def test_distillation_runs_on_the_utility_model_not_the_reviewer(
    tmp_path, store
):
    """D21: `_distill_large_chunks` read llm.review_model, so every oversized
    context chunk spent one Opus session to produce a summary only the coder
    ever reads. The reviewer's gate never sees it."""
    seen: list[str] = []

    class _Backend:
        def __init__(self, *, model, readonly=False, **_):
            seen.append(model)

        async def run(self, prompt, *, cwd, max_turns, effort=None, **kwargs):
            return AgentResult(final_text="a short summary", num_turns=1,
                               is_error=False, tokens_used=0, session_id="s",
                               stop_reason="end_turn")

    def _fake_advisory_backend(model, *, role):
        return _Backend(model=model)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    chunk = _Chunk("x" * (orch._CHUNK_DISTILL_THRESHOLD + 1))
    t = Task.new("t", repo_path=str(tmp_path))

    with _patch("no_human.core.orchestrator.advisory_backend",
                _fake_advisory_backend):
        await orch._distill_large_chunks([chunk], t)

    assert seen == [cfg.data["llm"]["utility_model"]]
    assert "opus" not in seen[0]
    assert chunk.content.startswith("[distilled]")


# --------------------------------------------------------------------------- #
# env_setup / env_vars / env_teardown                                          #
# --------------------------------------------------------------------------- #

async def test_env_vars_injected_during_agent_run(bare_repo, tmp_path, store):
    """env_vars in task.config are visible to the agent and restored after."""
    import os
    sentinel_key = "_NH_TEST_ENV_SENTINEL"
    assert sentinel_key not in os.environ  # clean slate

    captured = {}

    class EnvCapturingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            captured["val"] = os.environ.get(sentinel_key)
            # Produce a file change so the task doesn't fail.
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, EnvCapturingBackend(), SlackNotifier(None))
    t = Task.new("test env", repo_path=str(bare_repo))
    t.config = {"env_vars": {sentinel_key: "hello_nh"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert captured["val"] == "hello_nh"
    # Cleaned up after
    assert sentinel_key not in os.environ


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_env_setup_failure_aborts_attempt(bare_repo, tmp_path, store):
    """A failing env_setup command should abort the attempt before the agent runs."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("test setup fail", repo_path=str(bare_repo))
    t.config = {"env_setup": ["exit 1"]}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    # Should fail/escalate due to setup failure
    assert outcome.status in (TaskStatus.FAILED, TaskStatus.ESCALATED)
    kinds = [e["kind"] for e in events]
    assert "env_setup_failed" in kinds


# --------------------------------------------------------------------------- #
# Subagent materialization                                                     #
# --------------------------------------------------------------------------- #

async def test_subagents_materialized_before_agent_run(bare_repo, tmp_path, store):
    """Built-in subagent .md files are written to .claude/agents/ before the agent runs."""
    agents_dir_existed = {}

    class CheckingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            # Check that subagent files exist DURING the agent run, in the tree
            # the agent was actually handed — by default an isolated worktree,
            # not the operator's checkout.
            agents_dir = cwd / ".claude" / "agents"
            agents_dir_existed["exists"] = agents_dir.exists()
            agents_dir_existed["researcher"] = (agents_dir / "no_human_researcher.md").exists()
            agents_dir_existed["md"] = (
                (agents_dir / "no_human_researcher.md").read_text()
                if agents_dir_existed["researcher"] else "")
            # Produce file changes so the task completes.
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, CheckingBackend(), SlackNotifier(None))
    t = Task.new("test subagents", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert agents_dir_existed.get("exists"), ".claude/agents/ dir not created"
    assert agents_dir_existed.get("researcher"), "no_human_researcher.md not materialized"

    # Verify the file content is valid YAML frontmatter + instructions.
    researcher_md = agents_dir_existed["md"]
    assert "name: no_human_researcher" in researcher_md
    assert "NEVER edit files" in researcher_md
    # The frontmatter key the agent-file format actually reads. This said
    # `allowed_tools:` for months — a key nothing consumes, i.e. a restriction
    # that only looks like one. The SDK-side AgentDefinition is authoritative
    # when both exist, which is why the deny-list lives there and not here.
    assert "tools: [Read, Grep, Glob, Bash]" in researcher_md, researcher_md
    assert "allowed_tools:" not in researcher_md
    # The operator's checkout is not where agent tooling gets dropped.
    assert not (bare_repo / ".claude" / "agents").exists()


def test_researcher_subagent_is_read_only_structurally_not_rhetorically():
    """R10: "NEVER edit files" in a prompt is a request, not a restriction.

    Pins the mechanical half — the allow-list, the write-tool deny-list, and
    the model/effort that used to be inherited from whatever session happened
    to spawn the subagent. The deny-list is anchored to ``guard.WRITE_TOOLS``
    rather than to a hand-copied list, so adding a write tool to the guard and
    not to the subagent fails here.
    """
    from no_human.agent import guard

    defs = Orchestrator._subagent_definitions()
    researcher = defs["no_human_researcher"]

    assert set(researcher.tools) == {"Read", "Grep", "Glob", "Bash"}
    assert guard.WRITE_TOOLS <= set(researcher.disallowedTools or []), (
        "every tool the guard classifies as a write must also be denied to "
        f"the researcher: guard={sorted(guard.WRITE_TOOLS)} "
        f"subagent={sorted(researcher.disallowedTools or [])}"
    )
    # Not inherited: a grep-and-report job pinned to its own tier and effort.
    assert researcher.model == "sonnet"
    assert researcher.effort == "low"
    assert researcher.maxTurns == 10
    # No read-only PermissionMode exists in this SDK; headless sessions cannot
    # prompt, so the mode stays and the restriction lives in disallowedTools.
    assert researcher.permissionMode == "bypassPermissions"


def test_researcher_restrictions_survive_sdk_serialization():
    """Non-vacuity: the fields above must reach the CLI, not just the dataclass.

    ``claude_agent_sdk/_internal/client.py`` sends agents as
    ``{k: v for k, v in asdict(agent_def).items() if v is not None}``. This
    reproduces that exact transform — a field the SDK drops (renamed, or set to
    None) would pass the field assertions above and change nothing at runtime.
    """
    from dataclasses import asdict

    researcher = Orchestrator._subagent_definitions()["no_human_researcher"]
    payload = {k: v for k, v in asdict(researcher).items() if v is not None}

    assert "disallowedTools" in payload, (
        "the write deny-list is dropped before it reaches the CLI: "
        f"sent keys={sorted(payload)}"
    )
    assert "Write" in payload["disallowedTools"]
    assert payload["model"] == "sonnet"
    assert payload["effort"] == "low"


def test_guard_still_blocks_a_researcher_write_that_slips_the_deny_list():
    """A tool deny-list is the optimization; the guard is the boundary. If the
    deny-list were dropped tomorrow, this is what still holds — so the guard
    rule may never be retired on the strength of the field above."""
    from no_human.agent import guard

    decision = guard.evaluate(
        "Write",
        {"file_path": ".env", "content": "x"},
        forbidden_paths=[".env", "secrets/", "*.key", "*.pem"],
        never_push_to=["main", "master", "release/*"],
    )
    assert not decision.allow, decision.reason


def test_bash_is_an_open_write_path_and_the_docstring_must_keep_saying_so():
    """Characterization, not endorsement — and the correction of a false claim.

    An earlier draft of ``_subagent_definitions``' docstring said the guard
    caught what a Bash redirect reaches around a tool deny-list with. It does
    not: ``guard.evaluate`` denies an ENUMERATED list of destructive commands
    and allows every ordinary shell write, in both the coder and the read-only
    mode. The researcher keeps ``Bash`` because a researcher that cannot grep is
    not a researcher, so the honest statement is "read-only by tool surface,
    with Bash open" — and this test is what makes that statement falsifiable.

    If someone later closes the shell write path, this test fails, and the fix
    is to update the docstring to match — not to delete the test.
    """
    from no_human.agent import guard

    kw = dict(forbidden_paths=[".env", "secrets/", "*.key", "*.pem"],
              never_push_to=["main", "master", "release/*"])
    open_writes = [
        "echo x > src/foo.py",
        "sed -i '' s/a/b/ src/foo.py",
        "cat a >> src/foo.py",
        "printf 'x' | tee src/foo.py",
    ]
    for cmd in open_writes:
        for readonly in (False, True):
            d = guard.evaluate("Bash", {"command": cmd}, readonly=readonly, **kw)
            assert d.allow, (
                f"the guard now denies {cmd!r} (readonly={readonly}). That is a "
                "real tightening — update the 'Bash remains an open write path' "
                "paragraph in Orchestrator._subagent_definitions to match."
            )
    # Non-vacuity: the same call path DOES deny its enumerated patterns, so the
    # allows above are a policy result and not a broken invocation.
    for cmd in ("rm -rf /tmp/x", "rm tests/test_foo.py"):
        assert not guard.evaluate("Bash", {"command": cmd}, **kw).allow, cmd


# --------------------------------------------------------------------------- #
# Verify skill materialization                                                 #
# --------------------------------------------------------------------------- #

async def test_verify_skill_materialized_with_test_cmd(bare_repo, tmp_path, store):
    """When a confirmed profile exists, a verify skill with the test_cmd is materialized."""
    from no_human.profile import ProjectProfile
    skill_found = {}

    class SkillCheckingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            skill_path = cwd / ".claude" / "skills" / "no_human_verify" / "SKILL.md"
            skill_found["exists"] = skill_path.exists()
            if skill_path.exists():
                skill_found["content"] = skill_path.read_text()
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    marker = bare_repo / ".verify_skill_ran"
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="custom",
        test_cmd=f"sh -c 'echo ran > {marker}; exit 0'",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, SkillCheckingBackend(), SlackNotifier(None))
    t = Task.new("test verify skill", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert skill_found.get("exists"), "verify skill not materialized"
    assert "echo ran" in skill_found["content"]
    assert "proven" in skill_found["content"].lower()


# --------------------------------------------------------------------------- #
# Compact instructions materialization                                         #
# --------------------------------------------------------------------------- #

async def test_compact_instructions_materialized(bare_repo, tmp_path, store):
    """Compact instructions (.claude/instructions.md) are written before the agent runs."""
    from no_human.profile import ProjectProfile
    instructions_found = {}

    class InstructionsCheckingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            inst_path = cwd / ".claude" / "instructions.md"
            instructions_found["exists"] = inst_path.exists()
            if inst_path.exists():
                instructions_found["content"] = inst_path.read_text()
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    marker = bare_repo / ".inst_test_ran"
    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="python",
        test_cmd=f"sh -c 'echo ran > {marker}; exit 0'",
        derived_from=["test"], proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, InstructionsCheckingBackend(), SlackNotifier(None))
    t = Task.new("test instructions", repo_path=str(bare_repo), kind="investigation")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert instructions_found.get("exists"), ".claude/instructions.md not created"
    content = instructions_found["content"]
    assert "python" in content.lower()
    assert "INVESTIGATION" in content.upper()


# --------------------------------------------------------------------------- #
# Intake evaluator for non-grill tasks (Phase 4)                               #
# --------------------------------------------------------------------------- #

async def test_intake_evaluator_runs_for_non_grill_tasks(
    bare_repo, tmp_path, store, monkeypatch,
):
    """Tasks without eval_result in context get intake evaluation during planning."""
    from no_human.intake.evaluator import EvalResult, EvalVerdict

    eval_called = {}

    async def fake_evaluate_spec(title, desc, criteria, *, backend=None, model=None,
                                 usage_sink=None):
        eval_called["yes"] = True
        return EvalResult(
            verdict=EvalVerdict.DECOMPOSE,
            dimensions={"bounded_scope": False},
            rationale="too large",
        )

    monkeypatch.setattr(
        "no_human.core.orchestrator.evaluate_spec", fake_evaluate_spec,
        raising=False,
    )
    # Also patch the import path used inside _drive.
    monkeypatch.setattr(
        "no_human.intake.evaluator.evaluate_spec", fake_evaluate_spec,
    )

    class SimpleBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    cfg.data.setdefault("planning", {})["enabled"] = False
    orch = Orchestrator(store, cfg.data, SimpleBackend(), SlackNotifier(None))
    t = Task.new("big compound task", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    refreshed = await store.get_task(t.id)
    assert eval_called.get("yes"), "evaluate_spec was not called"
    assert refreshed.context.get("eval_result") is not None
    assert refreshed.context["eval_result"]["verdict"] == "decompose"


async def test_intake_evaluator_skipped_when_already_evaluated(
    bare_repo, tmp_path, store, monkeypatch,
):
    """Tasks that already have eval_result (grill path) skip re-evaluation."""
    eval_called = {}

    async def fake_evaluate_spec(title, desc, criteria, *, backend=None, model=None,
                                 usage_sink=None):
        eval_called["yes"] = True

    monkeypatch.setattr(
        "no_human.intake.evaluator.evaluate_spec", fake_evaluate_spec,
    )

    class SimpleBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    cfg.data.setdefault("planning", {})["enabled"] = False
    orch = Orchestrator(store, cfg.data, SimpleBackend(), SlackNotifier(None))
    t = Task.new("already evaluated", repo_path=str(bare_repo))
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    await orch.run_task(t)

    assert not eval_called.get("yes"), "evaluate_spec should not be called again"


async def test_intake_evaluator_failure_does_not_block_pipeline(
    bare_repo, tmp_path, store, monkeypatch,
):
    """Evaluator failure is advisory — task proceeds normally."""
    async def failing_evaluate_spec(title, desc, criteria, *, backend=None, model=None,
                                    usage_sink=None):
        raise RuntimeError("evaluator crashed")

    monkeypatch.setattr(
        "no_human.intake.evaluator.evaluate_spec", failing_evaluate_spec,
    )

    class SimpleBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = _config(tmp_path)
    cfg.data.setdefault("planning", {})["enabled"] = False
    orch = Orchestrator(store, cfg.data, SimpleBackend(), SlackNotifier(None))
    t = Task.new("evaluator crash test", repo_path=str(bare_repo))
    await store.create_task(t)

    outcome = await orch.run_task(t)
    # Task proceeds past evaluator failure — not stuck.
    assert outcome is not None


# --------------------------------------------------------------------------- #
# Which model ran which role is recorded (the blind spot that hid config drift) #
# --------------------------------------------------------------------------- #

@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_attempt_records_the_model_bound_to_each_role(bare_repo, tmp_path, store):
    def mutate(cwd):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    cfg = _config(tmp_path)
    events: list[dict] = []
    backend = FakeBackend(mutate)
    backend.model = "claude-sonnet-5"
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("tweak add()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    # 1. Emitted, so it lands in the log, the board and task_events.
    ev = next(e for e in events if e["kind"] == "models")
    assert ev["models"]["coder"] == "claude-sonnet-5"
    assert ev["models"]["planner"] == cfg.data["llm"]["planner_model"]
    assert "claude-sonnet-5" in ev["text"]

    # 2. Persisted on the attempt row.
    rows = await store.db.execute(
        "SELECT models FROM attempts WHERE task_id = ?", (t.id,))
    row = await rows.fetchone()
    assert json.loads(row["models"])["coder"] == "claude-sonnet-5"


def test_active_models_reads_the_live_objects_not_the_config(tmp_path, store):
    """Reading config is exactly what hid the drift: a frozen config.yaml
    shadows the default, so config and reality disagreed for a week."""
    cfg = _config(tmp_path)
    cfg.data["llm"]["primary_model"] = "a-model-that-is-not-actually-running"

    backend = FakeBackend(lambda cwd: None)
    backend.model = "the-model-really-bound"
    reviewer = _SimpleNamespace(model="reviewer-really-bound")
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None), reviewer=reviewer)

    models = orch._active_models()
    assert models["coder"] == "the-model-really-bound"
    assert models["reviewer"] == "reviewer-really-bound"


async def test_models_are_recorded_before_planning_not_just_at_attempt_start(
    bare_repo, tmp_path, store,
):
    """A task killed during planning must still say which model held which role.
    Observed live: 166 events survived a SIGKILL and not one named a model,
    because the only `models` event fired at attempt start."""
    cfg = _config(tmp_path)
    events: list[dict] = []
    backend = FakeBackend(lambda cwd: None)
    backend.model = "claude-sonnet-5"
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)

    await orch.run_task(t)

    kinds = [e["kind"] for e in events]
    first_models = kinds.index("models")
    assert first_models < kinds.index("state"), "models must precede context/planning"
    assert events[first_models]["models"]["coder"] == "claude-sonnet-5"


async def test_supervisor_model_is_recorded_and_is_not_the_reviewers(tmp_path, store):
    """It must be visible which model supervises: the role used to inherit
    review_model and nothing recorded that it had."""
    cfg = _config(tmp_path)
    backend = FakeBackend(lambda cwd: None)
    backend.model = "claude-sonnet-5"
    reviewer = _SimpleNamespace(model=cfg.data["llm"]["review_model"])
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        reviewer=reviewer)

    models = orch._active_models()
    assert models["supervisor"] == "claude-sonnet-5"
    assert models["reviewer"] == "claude-opus-4-8"
    assert models["supervisor"] != models["reviewer"], (
        "the supervisor must no longer inherit the reviewer's tier"
    )


def test_no_size_cap_by_default(tmp_path, store):
    """A line/file count cannot tell a legitimately large change from a runaway
    refactor, and the check runs after the commit — so it never saved compute, it
    only stopped lint, tests, the reviewer and the PR from running. Task 84251cb2
    wrote a correct 645-line Jenkinsfile stage and was escalated for it.

    Scope is guarded semantically instead: the plan's FILES TO CHANGE list, the
    tamper guard, the evidence-based reviewer, and the human approving the PR."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    huge = _SimpleNamespace(insertions=10_000, deletions=5_000, files_changed=300)

    assert cfg.data["safety"]["max_lines_changed"] is None
    assert cfg.data["safety"]["max_files_changed"] is None
    assert orch._over_size_limits(huge, Task.new("big", repo_path="/tmp/x")) is None


def test_an_opted_in_cap_still_escalates(tmp_path, store):
    """The cap is off, not gone: an install that wants one still gets it."""
    cfg = _config(tmp_path)
    cfg.data["safety"]["max_lines_changed"] = 500
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    commit = _SimpleNamespace(insertions=605, deletions=0, files_changed=3)

    assert "max_lines_changed (605 > 500)" in orch._over_size_limits(
        commit, Task.new("ci_gate", repo_path="/tmp/x")
    )


def test_a_non_positive_cap_means_unlimited(tmp_path, store):
    """0 is a natural way to spell "no cap"; it must not block every commit."""
    cfg = _config(tmp_path)
    cfg.data["safety"] = {"max_files_changed": 0, "max_lines_changed": 0}
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    commit = _SimpleNamespace(insertions=1, deletions=0, files_changed=1)

    assert orch._over_size_limits(commit, Task.new("t", repo_path="/tmp/x")) is None


def test_size_limits_honour_a_per_task_override(tmp_path, store):
    """The SCOPE_EXPLOSION blocker offers the human 'raise the limit for this
    task', but the limit was read from global config alone — answering that way
    produced the identical blocker on the next attempt."""
    cfg = _config(tmp_path)
    cfg.data["safety"] = {"max_files_changed": 20, "max_lines_changed": 500}
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    commit = _SimpleNamespace(insertions=605, deletions=0, files_changed=3)

    t = Task.new("ci_gate", repo_path="/tmp/x")
    assert "max_lines_changed (605 > 500)" in orch._over_size_limits(commit, t)

    t.config = {"max_lines_changed": 800}
    assert orch._over_size_limits(commit, t) is None

    # The file limit is independent and still applies.
    t.config = {"max_lines_changed": 800, "max_files_changed": 2}
    assert "max_files_changed (3 > 2)" in orch._over_size_limits(commit, t)

    # No task, or no override: global config governs.
    assert "605 > 500" in orch._over_size_limits(commit, None)


def test_scope_explosion_option_action_derives_from_the_observed_size(tmp_path, store):
    """D14: the option 'raise the limit for this task' must carry the limit that
    actually lets this commit through — rounded up from what it measured, never
    a hardcoded number, and only for the limit that was breached."""
    cfg = _config(tmp_path)
    cfg.data["safety"] = {"max_files_changed": 20, "max_lines_changed": 500}
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None), SlackNotifier(None))
    t = Task.new("ci_gate", repo_path="/tmp/x")

    # 605 lines over a 500 limit → 700. Files are within limit → left alone.
    action = orch._size_override_action(
        _SimpleNamespace(insertions=605, deletions=0, files_changed=3), t)
    assert action == {"set_task_config": {"max_lines_changed": 700}}

    # Only files breached → only the file limit is offered.
    action = orch._size_override_action(
        _SimpleNamespace(insertions=10, deletions=0, files_changed=25), t)
    assert action == {"set_task_config": {"max_files_changed": 25}}

    # Applying the action clears the very gate that produced it.
    from no_human.blockers import apply_action
    commit = _SimpleNamespace(insertions=605, deletions=0, files_changed=3)
    assert orch._over_size_limits(commit, t) is not None
    apply_action(t, orch._size_override_action(commit, t))
    assert orch._over_size_limits(commit, t) is None


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_reply_resumes_from_the_wip_blocked_checkpoint(bare_repo, tmp_path, store):
    """D15: the blocker printed 'Resume with: nh reply <id>', but the resume path
    read ctx['handoff']['wip_sha'] — written only when an attempt runs out of
    turns — and gated it on attempt_n > 1, which a resumed run never reaches
    because it restarts its numbering at 1. The checkpoint was discarded and 41
    turns were re-done from base."""
    from no_human.blockers import resume_checkpoint

    def leave_wip(cwd):
        (cwd / "wip_marker.py").write_text("# many turns of work\n")

    bjson = (
        '{"category": "DEPENDENCY_WAIT", "confidence": 0.9, '
        '"wake_condition": "pr_merged:org/repo#42", '
        '"root_cause_hypothesis": "needs #42", "goal": "g", "evidence": "e"}'
    )
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, BlockerBackend(bjson, mutate=leave_wip),
                        SlackNotifier(None))
    t = Task.new("resume me", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)

    blocked = await store.get_task(t.id)
    assert blocked.status is TaskStatus.BLOCKED
    checkpoint = resume_checkpoint(blocked.blocker)
    assert checkpoint and checkpoint["sha"]

    # Exactly what `nh reply` now does before handing the task back to the loop.
    ctx = blocked.context or {}
    ctx["resume_from"] = checkpoint
    blocked.context = ctx
    await store.update_task(blocked)
    await store.set_status(blocked, TaskStatus.IMPLEMENTING, validate=False)

    events: list[dict] = []
    orch2 = Orchestrator(
        store, cfg.data,
        FakeBackend(lambda cwd: (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a - b\n")),
        SlackNotifier(None), event_sink=events.append,
    )
    await orch2.run_task(blocked)

    assert any(e.get("kind") == "resume_wip" for e in events), \
        "the resumed attempt must branch from the [WIP-BLOCKED] checkpoint"

    attempts = await store.list_attempts(blocked.id)
    resumed = attempts[-1]
    # The work survived: the checkpoint is an ancestor of the resumed branch.
    tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", resumed["branch_name"]],
                          cwd=bare_repo, capture_output=True, text=True).stdout
    assert "wip_marker.py" in tree, "the checkpointed work was thrown away"

    # Branch names never collide, or `git checkout -B` would reset the branch
    # holding the checkpoint and destroy it.
    names = [a["branch_name"] for a in attempts if a["branch_name"]]
    assert len(names) == len(set(names)), f"branch names collided: {names}"
    assert [a["attempt_number"] for a in attempts] == list(range(1, len(attempts) + 1))


def test_review_base_is_the_merge_base_not_head_parent(tmp_path, store):
    """A resumed attempt carries the [WIP-BLOCKED] commit on its own branch, so
    HEAD~1 would show the reviewer only the delta over the checkpoint."""
    from no_human.vcs.git import GitRepo

    work = tmp_path / "r"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "a.txt").write_text("1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                              capture_output=True, text=True).stdout.strip()
    _git(work, "checkout", "-b", "feature")
    (work / "a.txt").write_text("2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-BLOCKED] partial")
    (work / "b.txt").write_text("3\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "finish")

    orch = Orchestrator(store, _config(tmp_path).data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    repo = GitRepo(work)
    assert orch._review_base(repo, "main") == base_sha  # whole change, both commits
    assert orch._review_base(repo, None) == "HEAD~1"  # no base → unchanged default
    assert orch._review_base(repo, "no-such-branch") == "HEAD~1"  # never blocks review


def test_pr_body_carries_the_review_evidence_dossier():
    """W1.6: the PR body is the human's review surface — the reviewer's
    verdict trail must be on it, not buried in the transcript."""
    from no_human.core.orchestrator import Orchestrator
    t = Task.new("dossier", repo_path="/tmp/x")
    t.context = {"review_history": [
        {"round": 1, "passed": False,
         "blocking": ["Image build failure treated as non-fatal",
                      "Zero tests reports PASSED"]},
        {"round": 2, "passed": True, "blocking": []},
    ]}
    section = Orchestrator._review_evidence_section(t)
    # The reviewer's verdict now leads the consolidated `## Evidence` area as its
    # `| Independent review |` row of the Evidence table.
    assert "| Independent review | ✅ **PASSED** — 2 rounds |" in section
    assert "**PASSED**" in section
    assert "Image build failure" in section
    # No review ran → section vanishes, body unchanged.
    t2 = Task.new("no-review", repo_path="/tmp/x")
    assert Orchestrator._review_evidence_section(t2) == ""
    # The DB stores context values as strings sometimes — survive that.
    t3 = Task.new("stringly", repo_path="/tmp/x")
    t3.context = {"review_history": "[{'round': 1, 'passed': True, 'blocking': []}]"}
    assert "**PASSED**" in Orchestrator._review_evidence_section(t3)


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_bugfix_without_repro_evidence_is_sent_back(bare_repo, tmp_path, store):
    """W1.2 (Agentless): a BUGFIX must prove the bug — a waived/failed repro
    verdict blocks before any reviewer tokens, with the fix instructions fed
    to the next attempt."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )
        # no repro manifest → verdict "waived"

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("fix mul() bug", repo_path=str(bare_repo), kind="bugfix")
    t.acceptance_criteria = ["mul works"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    gate = [e for e in events if e["kind"] == "repro_gate"]
    assert gate and gate[0]["verdict"] == "waived"
    assert "[required]" in gate[0]["text"]
    # The attempt died at the gate — before review — and the coder got told.
    fresh = await store.get_task(t.id)
    fb = (fresh.context or {}).get("send_back_feedback") or []
    assert any(f.get("source") == "repro_gate" for f in fb)
    assert any("repro gate waived" in (a.get("failure_reason") or "")
               for a in await store.list_attempts(t.id))
    assert outcome.status is not TaskStatus.AWAITING_APPROVAL


async def test_feature_with_waived_repro_still_proceeds(bare_repo, tmp_path, store):
    """The gate is advisory for non-bugfix kinds — a feature without a repro
    manifest must flow to review/PR exactly as before (conservative
    enforcement: classification decides, never the gate)."""
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))  # kind=feature
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    gate = [e for e in events if e["kind"] == "repro_gate"]
    assert gate and "[advisory]" in gate[0]["text"]


def test_failed_tests_event_carries_the_output_tail():
    """Triage 2026-07-11: 'FAIL: 0 passed, 0 failed, 1 errors' with no
    detail cost an hour of reproduction. The failure event must name the
    failing thing — assert via the emit-shaping logic used at the call site."""
    from types import SimpleNamespace
    result = SimpleNamespace(ok=False, output="x" * 50 + "\nImportError: cannot import name 'Foo' from 'bar'")
    fail_tail = (getattr(result, "output", "") or "")[-1200:]
    assert "ImportError" in fail_tail
    ok_result = SimpleNamespace(ok=True, output="all good")
    tail2 = "" if ok_result.ok else ok_result.output
    assert tail2 == ""


async def test_worktree_tasks_resolve_the_primary_repos_profile(bare_repo, tmp_path, store):
    """First parallel run (2026-07-11): all three worktree tasks lost their
    proven test command because the profile lookup used the WORKTREE path —
    the DB row is keyed by the primary path. Worktrees must resolve through
    git's common-dir to the primary."""
    import subprocess as sp
    from no_human.core.orchestrator import Orchestrator

    primary = str(bare_repo)
    wt = tmp_path / "wt"
    sp.run(["git", "-C", primary, "worktree", "add", str(wt), "HEAD"],
           capture_output=True, check=True)
    try:
        resolved = Orchestrator._primary_repo_path(str(wt))
        assert resolved == primary
        # The primary itself resolves to None (already primary).
        assert Orchestrator._primary_repo_path(primary) is None
    finally:
        sp.run(["git", "-C", primary, "worktree", "remove", "--force", str(wt)],
               capture_output=True)


def test_out_of_scope_becomes_a_forbidden_block_in_the_prompt():
    """W3.5 (agent-a playbook): the spec's out_of_scope is surfaced to the coder
    as a hard FORBIDDEN constraint, from the first attempt — not discovered
    late by the reviewer as scope creep."""
    from no_human.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    t = Task.new("add helper", repo_path="/tmp/x")
    t.context = {"spec": {
        "test_plan": "cover the happy path",
        "out_of_scope": ["do not touch the auth module",
                         "do not change the DB schema"],
    }}
    digest = orch._resume_digest(t)
    assert "OUT OF SCOPE" in digest
    assert "do not touch the auth module" in digest
    assert "do not change the DB schema" in digest
    # No out_of_scope → no forbidden block.
    t2 = Task.new("y", repo_path="/tmp/x")
    t2.context = {"spec": {"test_plan": "x"}}
    assert "OUT OF SCOPE" not in orch._resume_digest(t2)


def test_pr_url_parts_delegates_to_canonical_parser():
    """EH2: there is ONE PR-URL grammar — vcs.pr_watcher.parse_pr_url — and it
    carries the host (forge, host, slug, number) so GHE/self-hosted resolve."""
    from no_human.vcs.pr_watcher import parse_pr_url
    assert parse_pr_url("https://code.example.com/dev/query-service/pull/7003") == \
        ("github", "code.example.com", "dev/query-service", 7003)
    assert parse_pr_url("https://gitlab.com/org/repo/-/merge_requests/42") == \
        ("gitlab", "gitlab.com", "org%2Frepo", 42)
    assert parse_pr_url("https://gitlab.acme.net/ci_gate/subgroup/metrics-core-service/-/merge_requests/7") == \
        ("gitlab", "gitlab.acme.net", "ci_gate%2Fsubgroup%2Fmetrics-core-service", 7)
    assert parse_pr_url("not a url") is None


class _PlaceholderReportBackend:
    """Report-only backend that returns a PLACEHOLDER non-answer — the degenerate
    deliverable C3 must reject (retry, then escalate; never DONE)."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(
            final_text="Done.", num_turns=1, is_error=False, tokens_used=10,
            session_id="s", stop_reason="end_turn",
        )


# No longer slow: the inadequate-report streak escalates after 2 attempts
# instead of spending investigation's full 8-attempt bound.
async def test_investigation_placeholder_report_does_not_complete_as_done(
    bare_repo, tmp_path, store
):
    """C3: a placeholder report ("Done.") is not a deliverable — the attempt
    fails, the bounded loop retries, and the task escalates honestly instead of
    marking a non-answer DONE."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, _PlaceholderReportBackend(),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("investigate the data drop", repo_path=str(bare_repo),
                 kind="investigation")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.DONE, (
        f"a placeholder report must not complete as DONE (got {outcome.status})")
    # the inadequacy was recorded on the record, not silently dropped
    assert any(e.get("kind") == "report_inadequate" for e in events)
    attempts = await store.list_attempts(t.id)
    assert any(a["status"] == "failed" for a in attempts)


async def test_repeated_inadequate_reports_escalate_at_two_not_at_the_bound(
    bare_repo, tmp_path, store
):
    """C3 follow-up: rejecting an inadequate report makes the loop RETRY, and
    `investigation` carries the wider 8-attempt bound — so an agent that cannot
    produce substance spent six more attempts proving it and then reported
    BUDGET_EXHAUSTED, a blocker that says nothing about why.

    Same argument as the zero-diff streak: a retry re-runs the same agent
    against the same request. One retry (which now carries the reason as
    feedback), then escalate carrying what the agent actually wrote."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, _PlaceholderReportBackend(),
                        SlackNotifier(None))
    t = Task.new("investigate the data drop", repo_path=str(bare_repo),
                 kind="investigation")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED, outcome.detail
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 2, (
        f"must stop after 2 inadequate reports, not run the full bound "
        f"(ran {len(attempts)})")

    blocker = outcome.task.blocker
    assert blocker["category"] == "AMBIGUITY"
    # The human must see WHAT the agent produced, not just "inadequate" twice.
    assert "Done." in blocker["evidence"]
    assert blocker["question"]


class _AlternatingDudBackend:
    """Delivers nothing, but alternates HOW.

    Odd attempts return a placeholder report (non-empty → the report branch
    rejects it as inadequate). Even attempts return an EMPTY final_text, which
    fails the report branch's `final_text.strip()` condition, falls through to
    the code path, and lands as zero-diff. Both are "this attempt delivered
    nothing"; only the label differs.
    """

    def __init__(self):
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        text = "Done." if self.calls % 2 else ""
        return AgentResult(
            final_text=text, num_turns=3, is_error=False, tokens_used=400,
            session_id="s", stop_reason="end_turn",
        )


async def test_alternating_dud_attempts_escalate_instead_of_burning_the_bound(
    bare_repo, tmp_path, store
):
    """Two counters that reset in each OTHER's `else` never trip.

    zero-diff and inadequate-report each escalate after 2 CONSECUTIVE
    occurrences, but each reset the other's counter. An agent alternating
    between the two therefore drove both back to 0 every attempt, so neither
    guard fired and the loop spent its entire bound — precisely the runaway the
    two guards exist to stop, reachable because the report branch requires a
    NON-EMPTY final_text, so an empty response falls through to the code path.

    They now share one "delivered nothing" streak; only the escalation message
    is kind-specific.
    """
    cfg = _config(tmp_path)
    backend = _AlternatingDudBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None))
    t = Task.new("investigate the data drop", repo_path=str(bare_repo),
                 kind="investigation")
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED, outcome.detail
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 2, (
        f"alternating duds must escalate at 2, not burn the "
        f"{orch.bounds.max_attempts}-attempt bound (ran {len(attempts)})")

    # PIN THE PREMISE. Without this the test cannot tell alternation from two
    # identical failures: route an empty final_text into the report branch —
    # a natural future change — and both attempts become inadequate-report,
    # this degenerates into a copy of the repeated-inadequate test above, and
    # it keeps passing while covering nothing.
    reasons = [a["failure_reason"] for a in attempts]
    assert reasons[0].startswith("inadequate report"), reasons
    assert reasons[1] == "agent produced no file changes", reasons

    # A REPORT task must get the report question, not "is this already
    # implemented?" — the escalation is chosen by task kind, not by which
    # dud happened to land last.
    blocker = outcome.task.blocker
    assert blocker
    assert "Done." in blocker["evidence"], blocker["evidence"]
    assert "already implemented" not in (blocker["question"] or "")


class _DesignDocBackend:
    """Report-only backend that returns a SUBSTANTIVE design document (a real
    design doc is inherently multi-paragraph; C3's adequacy guard rejects a
    stub-length one, so this fixture reflects a genuine deliverable)."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        doc = (
            "# Retention pipeline design\n\n"
            "## Problem\nThe medstarhr instance stopped sending events after a "
            "retention policy misconfiguration; the pipeline silently drops late "
            "events instead of surfacing the gap.\n\n"
            "## Options\n1. Tighten the retention policy and alert on event-gap.\n"
            "2. Add a dead-letter buffer so late events replay.\n\n"
            "## Recommendation\nOption 2 plus a gap alert: it removes silent data "
            "loss and is observable, at the cost of a small replay buffer.\n"
        )
        return AgentResult(
            final_text=doc, num_turns=5, is_error=False, tokens_used=500,
            session_id="s", stop_reason="end_turn",
        )


async def test_design_doc_report_only_completes_as_done(bare_repo, tmp_path, store):
    """A design_doc task is a READ-ONLY deliverable: findings (the document)
    with no file changes complete as DONE — reusing the investigation rails."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, _DesignDocBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("Write a design doc for the retention pipeline",
                 repo_path=str(bare_repo), kind="design_doc")
    t.acceptance_criteria = ["document covers options and a recommendation"]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE, f"expected DONE, got {outcome.status}"
    assert "report-only" in outcome.detail
    # v7 live find: the judge was fed the placeholder detail because the DONE
    # outcome never carried the findings — the #85 bug class on this terminal.
    # The deliverable must ride on outcome.report, like every other producer.
    assert "medstarhr" in outcome.report
    refreshed = await store.find_task(t.id)
    assert "findings" in (refreshed.context or {})
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "succeeded"


async def test_design_doc_with_test_bearing_criteria_escalates_instead_of_completing(
    bare_repo, tmp_path, store
):
    """Defect 204f2177 (red-first repro): a design_doc-classified task whose
    acceptance criteria demand a CLI flag with red-first tests must never
    silently complete via the report-only path — it escalates to a human
    instead of being marked DONE on a report that never ships the demanded
    artifact."""
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, _DesignDocBackend(), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("Add nh approve --landed", repo_path=str(bare_repo), kind="design_doc")
    t.acceptance_criteria = [
        "nh approve --landed marks the PR landed",
        "a red-first test proves the flag's behaviour before the fix",
        "a control test proves the flag is a no-op without --landed",
    ]
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is not TaskStatus.DONE, (
        f"a report-only completion must never silently satisfy test-bearing "
        f"criteria — got {outcome.status}")
    assert outcome.status is TaskStatus.AWAITING_INPUT
    refreshed = await store.find_task(t.id)
    assert refreshed.status is TaskStatus.AWAITING_INPUT
    assert refreshed.blocker is not None
    assert refreshed.blocker.get("category") == "AMBIGUITY"
    kinds = [e["kind"] for e in events]
    assert "kind_criteria_mismatch" in kinds


class _CleanReviewer:
    """Reviewer stub: passes the review with zero findings."""

    async def review(self, task, **kwargs):
        # The real type, not a SimpleNamespace: this stub used to hand-roll a
        # subset of ReviewDecision and silently lacked `blocking_items` /
        # `advisory_items`, so a caller that read them crashed only here and
        # only in tests. Building the real thing cannot drift out of contract.
        from no_human.review.reviewer import ReviewDecision
        return ReviewDecision(
            passed=True, checklist=[],
            raw_output="Reviewed thoroughly: LGTM, no defects found.",
            tokens_used=10, cache_read_tokens=0, cache_creation_tokens=0,
        )


async def test_clean_code_review_done_carries_the_review_as_report(
    bare_repo, tmp_path, store, monkeypatch
):
    """v7 live find (placeholder-bug class): a clean-pass code review ended
    DONE with only the terse draft-count detail — the REVIEW itself is the
    deliverable and must ride on outcome.report, symmetric with the
    drafts→AWAITING_APPROVAL producer."""
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), reviewer=_CleanReviewer())
    t = Task.new("review this PR https://forge.example/x/y/pull/42",
                 repo_path=str(bare_repo), kind="code_review")
    await store.create_task(t)
    monkeypatch.setattr(orch, "_fetch_pr_diff",
                        lambda repo, url: "diff --git a/f.py b/f.py\n+x = 1\n")

    async def _no_comments(pr_url):
        return ""
    monkeypatch.setattr(orch, "_fetch_pr_comments_text", _no_comments)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.DONE
    assert "LGTM" in outcome.report


async def test_planner_prompt_carries_intake_qa(bare_repo, tmp_path, store):
    """§6 grill: the plan is built on the question-answered spec — every
    base-prompt consumer (single planner + MoA proposers) sees the intake
    Q&A block (review r1 f1: it previously reached only the coder)."""
    cfg = _planning_config(tmp_path)
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None))
    t = Task.new("grilled", repo_path=str(bare_repo))
    t.context = {"intake_qa": [
        {"question": "Which file carries the fix?", "decision_it_changes":
         "target", "answer": "src/x.py:1", "source": "repo-evidence",
         "carve_out": "none"}]}
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    base = _base_prompts(backend)
    assert base
    assert all("RESOLVED AT INTAKE" in p for p in base)
    assert all("src/x.py:1" in p for p in base)


class _AlwaysEmptyBackend:
    """Never produces text — every attempt lands as zero-diff."""

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(
            final_text="", num_turns=2, is_error=False, tokens_used=300,
            session_id="s", stop_reason="end_turn",
        )


async def test_stale_report_text_cannot_hijack_a_zero_diff_escalation(
    bare_repo, tmp_path, store
):
    """`inadequate_report_text` lives on task.context and is NEVER cleared, so
    keying the escalation off its presence read evidence from an earlier
    attempt — or from an earlier bounded loop entirely, since context survives
    escalate -> `nh reply` -> fresh loop.

    A report task whose attempts are BOTH genuine zero-diffs must get the
    zero-diff escalation, even when a previous run left report text behind.
    Otherwise the human is told two attempts "returned a report" when neither
    did, and is shown text from a run that already ended.
    """
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, _AlwaysEmptyBackend(), SlackNotifier(None))
    t = Task.new("investigate the data drop", repo_path=str(bare_repo),
                 kind="investigation")
    t.context = {"inadequate_report_text": "STALE TEXT FROM A PREVIOUS RUN",
                 "inadequate_report_reason": "stale reason"}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.ESCALATED, outcome.detail
    blocker = outcome.task.blocker
    assert blocker
    assert "STALE TEXT FROM A PREVIOUS RUN" not in blocker["evidence"], blocker
    assert "without editing any file" in blocker["root_cause_hypothesis"], blocker


async def test_layered_failure_stuck_note_precedes_excerpt_block(
    bare_repo, tmp_path, store,
):
    """Review 2026-07-25 residue: the stuck-note is a one-line triage summary
    and must come BEFORE the (multi-KB) excerpt block in the failure detail,
    not be buried under it."""
    from no_human.core.bounds import StuckDetector
    from no_human.testing.test_layers import Gating, TestLayer, TestPlan
    from no_human.testing.plan_runner import LayerResult, PlanResult
    from no_human.testing.runner import TestRunResult
    import no_human.testing.plan_runner as plan_runner_mod

    plan = TestPlan(layers=[
        TestLayer(name="unit", command="pytest -q", gating=Gating.BLOCKING),
    ])
    tr = TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command="pytest -q", output="1 failed: identical signature",
        traceback_excerpts={
            "test_unit.py::test_a": "AssertionError: Root Cause Here",
        },
    )
    lr = LayerResult(layer_name="unit", gating=Gating.BLOCKING, result=tr)

    def fake_run_test_plan(test_plan, task_repo, **kwargs):
        return PlanResult(layer_results=[lr])

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("stuck ordering layered", repo_path=str(bare_repo))
    await store.create_task(t)

    async def fake_resolve_test_plan(task):
        return plan

    def primed_detector():
        d = StuckDetector()
        # edit-loop priming: _edit_counts only accumulates (the agent phase
        # resets consecutive-repeat counts on every distinct tool call).
        d._edit_counts = {"calc.py": d.edit_threshold}
        return d

    import no_human.core.orchestrator as orch_mod
    with _patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         _patch.object(plan_runner_mod, "run_test_plan", fake_run_test_plan), \
         _patch.object(orch_mod, "StuckDetector", primed_detector):
        await orch.run_task(t)

    failed_events = [e for e in events if e.get("kind") == "attempt_failed"]
    assert failed_events
    detail = failed_events[-1]["text"]
    note = "edit-loop: calc.py edited"
    assert note in detail, detail[-300:]
    assert "Root Cause Here" in detail
    assert detail.index(note) < detail.index("Root Cause Here"), (
        "stuck note must precede the excerpt block")


async def test_single_command_failure_stuck_note_precedes_excerpt_block(
    bare_repo, tmp_path, store,
):
    """Same ordering guarantee for the single-command (non-layered) path."""
    from no_human.core.bounds import StuckDetector
    from no_human.testing.runner import TestRunResult

    tr = TestRunResult(
        ran=True, ok=False, passed=0, failed=1, errors=0,
        command="pytest -q", output="1 failed: identical signature",
        traceback_excerpts={
            "test_unit.py::test_a": "AssertionError: Root Cause Here",
        },
    )

    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("stuck ordering single", repo_path=str(bare_repo))
    await store.create_task(t)

    async def fake_run_tests_once(repo, cmd, cwd=None):
        return tr, False

    async def fake_resolve_test_plan(task):
        return None

    def primed_detector():
        d = StuckDetector()
        # edit-loop priming: _edit_counts only accumulates (the agent phase
        # resets consecutive-repeat counts on every distinct tool call).
        d._edit_counts = {"calc.py": d.edit_threshold}
        return d

    import no_human.core.orchestrator as orch_mod
    with _patch.object(orch, "_resolve_test_plan", fake_resolve_test_plan), \
         _patch.object(orch, "_run_tests_once", fake_run_tests_once), \
         _patch.object(orch_mod, "StuckDetector", primed_detector):
        await orch.run_task(t)

    failed_events = [e for e in events if e.get("kind") == "attempt_failed"]
    assert failed_events
    detail = failed_events[-1]["text"]
    note = "edit-loop: calc.py edited"
    assert note in detail, detail[-300:]
    assert "Root Cause Here" in detail
    assert detail.index(note) < detail.index("Root Cause Here"), (
        "stuck note must precede the excerpt block")


# --------------------------------------------------------------------------- #
# A config that ASKS for CI but cannot build a backend must not be silent       #
# --------------------------------------------------------------------------- #

async def test_a_ci_block_that_cannot_build_a_backend_is_announced(bare_repo, tmp_path, store):
    """`ci.enabled: true` + no pipeline target must not pass unremarked.

    Two defects met here, and both were found by the adoption harness before
    either was fixed. `ci_from_config` returns None — not an error — when the
    selected backend's required key is absent (KNOWN_ISSUES KI-5), and the
    global `ci:` block that `docs/configuration.md` documents was read by
    nothing at all, so the documented way to configure a gate produced no gate
    and no diagnostic. A user who configured CI, got one key wrong, and was
    therefore NOT gated, saw nothing: no event, no blocker, and a `ci_skipped`
    message saying "no remote CI configured" — the opposite of what happened.

    `Orchestrator._resolve_ci_runner` now reads the global block and emits an
    `advisory` when a source claims CI and cannot be built. This asserts that
    end to end, through `run_task`, from the config a user actually writes —
    the unit tests for the resolver live in `tests/test_ci.py`, and a resolver
    that is right in isolation is not the same claim as a run that reports it.

    What is asserted HERE is only that the situation is VISIBLE, because
    invisibility is what let it survive. Visible was not the same as binding:
    the run below still opened an ungated PR, which is asserted — and now
    prevented — by `test_ci_enabled_without_a_target_does_not_open_an_ungated_pr`
    at the end of this file. This test keeps its narrower claim on purpose; if
    the escalation is ever relaxed, the advisory must still fire.
    """
    def mutate(cwd):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")

    cfg = _config(tmp_path)
    # Exactly what docs/configuration.md tells a user to write, minus the one
    # key GitLab needs. This is the shape that used to be read by nothing.
    cfg.data["ci"] = {"enabled": True, "backend": "gitlab",
                      "hostname": "gitlab.example"}

    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add add()", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)

    kinds = [e["kind"] for e in events]
    advisories = [e for e in events if e["kind"] == "advisory"
                  and "CI backend configured" in e.get("text", "")]
    assert advisories, (
        "a global `ci:` block asking for CI whose backend could not be built "
        "produced no advisory — this is exactly the silence KI-5 is about\n"
        f"events: {kinds}")
    text = advisories[0]["text"]
    # The message has to name the origin and the cause, or it is a different
    # kind of silence: "something is wrong somewhere" is not actionable.
    assert "global config" in text, text
    assert "gitlab" in text, text
    assert "NO CI gate" in text, text

    # And the honest-reporting event must no longer claim CI was unconfigured.
    skipped = [e for e in events if e["kind"] == "ci_skipped"]
    if skipped:
        assert "no remote CI configured" not in skipped[0]["text"], (
            "`ci_skipped` still claims CI was not configured, on a run where it "
            "WAS configured and merely unbuildable")


async def test_no_ci_block_at_all_stays_silent_and_proceeds(bare_repo, tmp_path, store):
    """The negative control, without which the test above proves nothing.

    A repo with no `ci` block has not asked for anything, so it must NOT get the
    advisory. If it fired for every ungated repo it would be noise within a week
    and would be muted, which is how the original defect became invisible in the
    first place. `DEFAULT_CONFIG["ci"]["enabled"]` is False, so this is also the
    assertion that an install which never configured CI is unaffected by the
    resolver reading the global block at all.
    """
    from no_human.profile import ProjectProfile

    def mutate(cwd):
        (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (cwd / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")

    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="python-pytest",
        test_cmd="pytest -q", derived_from=["test"],
        proven={"test_cmd": True}, confirmed=True,
    )
    await store.upsert_profile(prof)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add add()", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)

    kinds = [e["kind"] for e in events]
    ci_advisories = [e for e in events if e["kind"] == "advisory"
                     and "CI backend configured" in e.get("text", "")]
    assert not ci_advisories, (
        "the advisory fired for a repo that never asked for CI — it would "
        f"become noise and be ignored\nevents: {[e['text'] for e in ci_advisories]}")
    # ...and prove the branch that WOULD emit was actually reached, so this
    # control cannot pass merely because the run stopped early.
    assert "profile" in kinds, (
        "the profile was never loaded, so this run never reached the code that "
        f"decides whether to emit — the control proves nothing\nevents: {kinds}")


# --------------------------------------------------------------------------- #
# A CI block that asks for a gate and cannot produce one must not open a PR.   #
#                                                                             #
# The advisory above made the failure VISIBLE; it did not make it BINDING.     #
# `_resolve_ci_runner` left `self.ci_runner = None`, and the only reader of    #
# that is `if self.ci_runner is None:` — which means "no remote CI is wired    #
# for this repo, the local suite is the only gate" and proceeds to open a PR.  #
# So a user who mistyped one key got exactly the run a user who deliberately   #
# declined CI gets: a PR, no gate, and the belief that the advertised gate     #
# ran. KNOWN_ISSUES KI-5, "what a fix has to prove" #1 and #2.                 #
# --------------------------------------------------------------------------- #

def _ci_mutate(cwd):
    """A REAL change (calc.py gains mul), so the control test reaches open_pr
    instead of tripping the zero-diff gate — which would make it pass for a
    reason that has nothing to do with CI."""
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n")


async def _run_with_ci_block(store, tmp_path, bare_repo, ci_block, *, kind="feature"):
    cfg = _config(tmp_path)
    cfg.data["ci"] = ci_block
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_ci_mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add add()", repo_path=str(bare_repo), kind=kind)
    await store.create_task(t)
    outcome = await orch.run_task(t)
    return outcome, events, t


@pytest.mark.parametrize("backend,missing", [
    ("gitlab", "project"),
    ("github_actions", "repo"),
])
async def test_ci_enabled_without_a_target_does_not_open_an_ungated_pr(
        backend, missing, bare_repo, tmp_path, store):
    """Both backends, through `run_task`, from the config a user writes."""
    outcome, events, t = await _run_with_ci_block(
        store, tmp_path, bare_repo,
        {"enabled": True, "backend": backend, "hostname": "ci.example"})

    kinds = [e["kind"] for e in events]
    assert outcome.status is TaskStatus.ESCALATED, (
        f"a run that asked for CI and had none still completed: {outcome.status} "
        f"({outcome.detail})\nevents: {kinds}")
    assert outcome.pr_url is None, "an ungated PR was opened anyway"
    assert "pr_open" not in kinds, f"a PR was opened: {kinds}"

    refreshed = await store.get_task(t.id)
    assert refreshed.status is TaskStatus.ESCALATED
    blocker = refreshed.blocker or {}
    assert blocker, "escalated with no blocker — the human has nothing to act on"
    # 22.4: the human fixes this in under a minute only if the report names the
    # key. "CI is misconfigured" costs them the same search the code just did.
    assert missing in json.dumps(blocker), json.dumps(blocker)
    assert backend in json.dumps(blocker), json.dumps(blocker)

    # The advisory is still emitted — this replaces nothing, it BINDS what was
    # already visible.
    assert [e for e in events if e["kind"] == "advisory"
            and "CI backend configured" in e.get("text", "")], kinds

    # ...and it BINDS BEFORE ANY METERED CALL. This is the assertion behind the
    # cost claim in the comment at the escalation site, which is otherwise a
    # sentence nothing tests: a first draft escalated below the spine, so the
    # trail read [..., 'planning', 'advisory', ...] and every deterministic
    # re-run bought a full MoA planning round to learn what was knowable from
    # a SQLite read. If any of these appears, the escalation has drifted back
    # down the spine and the claim is false again.
    for spent in ("planning", "attempt_start", "prompt_size", "tool_use",
                  "knowledge_accessed", "skills_loaded"):
        assert spent not in kinds, (
            f"{spent!r} ran before the CI escalation — it is below a metered "
            f"call again\nevents: {kinds}")
    assert [e for e in events if e["kind"] == "state"
            and e.get("status") in ("context", "planning")] == [], kinds


async def test_profile_ci_hint_with_no_global_block_still_opens_a_pr(
        bare_repo, tmp_path, store):
    """The gap this fix does NOT close, pinned end to end so it is deliberate.

    `nh onboard` writes a bare `{"backend": "gitlab"}` on seeing a
    `.gitlab-ci.yml` — a detection hint, not a request for a gate — so
    `_resolve_ci_runner` does not treat it as a CI source and the run proceeds
    ungated with no advisory and no escalation. A user who onboarded and then
    MISTYPED `project` in the profile lands here too, which is the same shape
    the escalation exists for. Recorded in KNOWN_ISSUES KI-5; see
    `test_profile_ci_with_no_target_and_no_global_block_is_not_a_source` in
    tests/test_ci.py for why closing it needs onboarding to record intent.

    If someone closes that gap, this test SHOULD fail. That is the point: it
    is a tripwire on a known-wrong behaviour, not an endorsement of it.
    """
    from no_human.profile import ProjectProfile

    prof = ProjectProfile(
        repo_path=str(bare_repo), ecosystem="python-pytest",
        test_cmd="pytest -q", derived_from=["test"],
        proven={"test_cmd": True}, confirmed=True,
        ci={"backend": "gitlab"},          # the bare detection hint
    )
    await store.upsert_profile(prof)

    cfg = _config(tmp_path)               # no `ci` block at all
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_ci_mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    await store.create_task(t)
    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert outcome.pr_url, "ungated, and that is the pinned (wrong) behaviour"
    assert not [e for e in events if e["kind"] == "advisory"
                and "CI backend configured" in e.get("text", "")]


@pytest.mark.parametrize("kind", ["code_review", "investigation", "design_doc"])
async def test_pr_less_kinds_are_not_escalated_for_a_broken_ci_block(
        kind, bare_repo, tmp_path, store):
    """A missing CI gate can only make a PR dishonest, and these open none.

    `doctor.py` already names these three as legitimately PR-less. Escalating
    one for a broken `ci:` block would park work the gate was never going to
    cover — a false positive the fix created for itself by moving the check to
    the top of `_drive`: below the kind branches, `code_review` returned before
    CI was ever resolved, so nothing had to think about it.
    """
    _, events, t = await _run_with_ci_block(
        store, tmp_path, bare_repo,
        {"enabled": True, "backend": "gitlab", "hostname": "ci.example"},
        kind=kind)

    refreshed = await store.get_task(t.id)
    blob = json.dumps(refreshed.blocker or {})
    assert "ci.project" not in blob, (
        f"a {kind} task was blocked on a CI gate it would never have used: {blob}")
    assert "IMPOSSIBLE" not in blob, blob


async def test_ci_deliberately_disabled_still_opens_a_pr(bare_repo, tmp_path, store):
    """The control that decides whether the fix is safe to ship.

    `ci.enabled: false` is a supported, common configuration — the shipped
    DEFAULT — and this same targetless block under it must stay silent, cheap
    and PR-opening. If this escalated, the fix would have turned every install
    that never configured CI into a parked task.
    """
    outcome, events, t = await _run_with_ci_block(
        store, tmp_path, bare_repo,
        {"enabled": False, "backend": "gitlab", "hostname": "ci.example"})

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    assert outcome.pr_url
    assert not [e for e in events if e["kind"] == "advisory"
                and "CI backend configured" in e.get("text", "")]
    assert [e for e in events if e["kind"] == "ci_skipped"], \
        "the honest 'no remote CI ran' signal must still fire"


async def test_planning_emits_what_steered_the_plan(bare_repo, tmp_path, store):
    """A human approving at the plan gate sees the plan, never what shaped it.

    The emit is the only record that repo-authored text entered the planner's
    context. It must name the files, and it must name the ones DROPPED by the
    aggregate cap — a file absent from the context is precisely the one an
    approver cannot otherwise know to ask about.

    It must NOT carry the file contents: those are already in the prompt, and
    duplicating them into the event stream would swamp it.
    """
    cfg = _planning_config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    # From the orchestrator's own list — see the note in test_repo_instructions.
    _f1, _f2, _f3 = Orchestrator._REPO_INSTRUCTION_FILES[:3]
    (bare_repo / _f1).write_text("c" * 20_000)
    (bare_repo / _f2).write_text("a" * 20_000)
    (bare_repo / _f3).write_text("CANARY_SHOULD_NOT_BE_EMITTED")
    t = Task.new("a task", repo_path=str(bare_repo))
    backend = PromptCapturingPlannerBackend(_SAMPLE_PLAN)

    with _patch("no_human.core.orchestrator.ClaudeBackend", return_value=backend):
        await orch._generate_plan(t, GitRepo(bare_repo))

    lines = [e.get("text", "") for e in events
             if "repo conventions used as context" in e.get("text", "")]
    assert len(lines) == 1, f"expected exactly one audit line, got {lines}"
    line = lines[0]
    assert _f1 in line and _f2 in line
    assert "truncated" in line
    assert "DROPPED" in line and _f3 in line, (
        f"a file the planner never saw was not named: {line}")
    assert "CANARY_SHOULD_NOT_BE_EMITTED" not in line, (
        "the audit line is leaking file CONTENT into the event stream")


# --------------------------------------------------------------------------- #
# A review gate that never RAN parks — end to end (2026-08-11, ad5cde99 /      #
# 7d63dbe1: two tasks sat escalated for hours over a quota outage).            #
# --------------------------------------------------------------------------- #

class UnavailableReviewer:
    """The gate's session dies instead of returning a verdict."""

    def __init__(self, message: str):
        self._message = message
        self.calls = 0

    async def review(self, task, **kwargs):
        from no_human.review.reviewer import ReviewerUnavailable

        self.calls += 1
        raise ReviewerUnavailable(self._message)


def _mutate_add_mul(cwd):
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


async def test_a_dead_reviewer_session_parks_the_whole_run(bare_repo, tmp_path,
                                                           store):
    """Driven through `run_task`, because the routing fix is only worth what
    the WIRING is worth: the attempt must reach `_escalate_reviewer_unavailable`
    with the repo and branch, or the park records no checkpoint and the resume
    throws the coder's finished work away."""
    from no_human.review.reviewer import REVIEW_SESSION_ERROR_MARKER

    cfg = _config(tmp_path)
    reviewer = UnavailableReviewer(
        "the reviewer reached no verdict after 2 rounds "
        f"({REVIEW_SESSION_ERROR_MARKER} reviewer session error (error) — "
        "You've hit your weekly limit. Your limit will reset at 3pm.)")
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate_add_mul),
                        SlackNotifier(None), event_sink=[].append,
                        reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    parked = await store.get_task(t.id)

    assert reviewer.calls, "the gate never ran — the test proves nothing"
    assert outcome.status is TaskStatus.PAUSED_QUOTA, (
        f"an outage burned a human escalation: {outcome.status} {outcome.detail}")
    assert parked.blocker["wake_condition"] == "quota_refreshed"
    assert parked.wake_check_at
    # ...and the coder's finished work is what the resume continues from.
    assert _SHA_RE.match(parked.blocker["resume_commit"] or ""), parked.blocker
    assert parked.blocker["resume_branch"].startswith("no-human/")
    # The diff did NOT pass: no PR was opened on an unreviewed change.
    assert not outcome.pr_url
    # DEFECT 2(b): the coder's own attempt row for this round must be closed
    # as infra-attributed, or this reviewer-side wall death is an unattributed
    # dead row to anything keyed on `infra_failure` — the dead-resume breaker
    # (`blockers/wake.py`, `_dead_resume_verdict`) among them.
    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt row was ever created — the test proves nothing"
    coder_attempt = attempts[-1]
    assert coder_attempt["infra_failure"] == 1, coder_attempt
    assert (coder_attempt["failure_reason"] or "").startswith("quota:"), (
        coder_attempt)


async def test_a_transient_infra_reviewer_park_classifies_its_attempt_row(
        bare_repo, tmp_path, store):
    """Sibling of the QUOTA-park test above, for the plain TRANSIENT_INFRA
    branch (`elif session_error:` in `_escalate_reviewer_unavailable` — the
    marker is present but the prose carries no quota signal and no bare
    `_BARE_SDK_RESULT_MARKER`, so it never reaches the QUOTA or corroborated-
    QUOTA branches). Same requirement: the coder's attempt row must be closed
    `infra_failure=1`, this time with an `infra:`-prefixed reason."""
    from no_human.review.reviewer import REVIEW_SESSION_ERROR_MARKER

    cfg = _config(tmp_path)
    reviewer = UnavailableReviewer(
        "the reviewer reached no verdict after 2 rounds "
        f"({REVIEW_SESSION_ERROR_MARKER} reviewer session error (error) — "
        "connection reset by peer)")
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate_add_mul),
                        SlackNotifier(None), event_sink=[].append,
                        reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    parked = await store.get_task(t.id)

    assert reviewer.calls, "the gate never ran — the test proves nothing"
    assert parked.blocker["category"] == "TRANSIENT_INFRA", parked.blocker
    assert parked.blocker["wake_condition"].startswith("after:"), parked.blocker
    assert not outcome.pr_url

    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt row was ever created — the test proves nothing"
    coder_attempt = attempts[-1]
    assert coder_attempt["infra_failure"] == 1, coder_attempt
    assert (coder_attempt["failure_reason"] or "").startswith("infra:"), (
        coder_attempt)


async def test_a_verdict_clears_the_consecutive_infra_park_streak(
        bare_repo, tmp_path, store):
    """The other half of the cap. The counter is what escalates a task after
    `_MAX_REVIEW_INFRA_PARKS` dead gates, so a run whose gate WORKS must clear
    it — otherwise three unrelated outages over a task's life eventually
    escalate a task whose reviewer is healthy."""
    from no_human.core.orchestrator import _REVIEW_INFRA_PARKS_KEY

    cfg = _config(tmp_path)
    reviewer = FakeReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("mul(a,b) implemented", True, "calc.py:4 returns a*b"),
    ]))
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate_add_mul),
                        SlackNotifier(None), event_sink=[].append,
                        reviewer=reviewer)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {_REVIEW_INFRA_PARKS_KEY: 2}
    await store.create_task(t)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome.detail
    done = await store.get_task(t.id)
    assert _REVIEW_INFRA_PARKS_KEY not in (done.context or {}), (
        "a working review gate left the infra-park streak standing")


_MANIFEST_REFUSAL = (
    "no_human pre-commit gate: REFUSED\n"
    "  src/pkg/mod.py\n"
    "    pinned abc123"
)


async def test_a_refusing_commit_fails_the_attempt_honestly_not_as_a_crash(
        bare_repo, tmp_path, store, monkeypatch):
    """R2/F4: the try/except around `commit_with_manifest_repair` (the seam
    itself) must turn a `GitError` into an honest attempt failure — never an
    uncategorized `task_crashed` (`scheduler.py` only emits that when
    `run_task` raises)."""
    from no_human.core import orchestrator as orch_mod
    from no_human.vcs import GitError

    def fake_commit_with_manifest_repair(repo, edited, commit_msg, on_repair=None):
        raise GitError(_MANIFEST_REFUSAL)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate_add_mul),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)  # must not raise

    kinds = [e["kind"] for e in events]
    assert "task_crashed" not in kinds, kinds
    assert "commit_refused" in kinds, kinds
    assert "commit" not in kinds, "no commit ever succeeded — none should be recorded"
    assert "manifest_repaired" not in kinds, (
        "no repair happened on this path — a spurious event was emitted")
    assert outcome.status is not TaskStatus.PENDING
    assert outcome.status != TaskStatus.AWAITING_APPROVAL

    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt was recorded"
    for a in attempts:
        assert a["status"] == "failed", a
        assert a["failure_reason"].startswith("commit failed:"), a["failure_reason"]
        assert "REFUSED" in a["failure_reason"], a["failure_reason"]


async def test_a_repair_before_a_second_refusal_is_still_on_the_record(
        bare_repo, tmp_path, store, monkeypatch):
    """R2: when `export_guard approve` SUCCEEDS (the release ledger is
    rewritten in the working tree — `on_repair` fires) and the retry commit
    then refuses AGAIN, the ledger mutation must never be silently absent
    from the task record: `manifest_repaired` must still be emitted even
    though the attempt ultimately fails with `commit_refused`."""
    from no_human.core import orchestrator as orch_mod
    from no_human.vcs import GitError

    approved_paths = ["src/pkg/mod.py", "tests/test_mod.py"]

    def fake_commit_with_manifest_repair(repo, edited, commit_msg, on_repair=None):
        if on_repair is not None:
            on_repair(approved_paths, "1 stale pin(s)")
        raise GitError(_MANIFEST_REFUSAL)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    cfg.data["bounds"] = {"max_attempts": 1}
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(_mutate_add_mul),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    await orch.run_task(t)  # must not raise

    kinds = [e["kind"] for e in events]
    # Regression guard on the exact bug: `if repaired:` used to sit
    # unreachable after the except-return, so the event count was 0 on this
    # path. It must be at least 1 — never dropped, never duplicated by the
    # finally on a success path that never happens here.
    #
    # It is exactly 2 here, not 1: with `max_attempts=1` the failed attempt's
    # commit refusal (via the mocked seam) is followed by a max-attempts
    # blocker whose checkpoint ALSO routes through the same mocked seam
    # (`_checkpoint_wip` now uses `commit_with_manifest_repair` too — the fix
    # under test) and fires `on_repair` again on its own refused retry. Both
    # firings are real ledger mutations and neither may be silently dropped.
    assert kinds.count("manifest_repaired") == 2, kinds
    repaired_events = [e for e in events if e["kind"] == "manifest_repaired"]
    for repaired_event in repaired_events:
        assert repaired_event["paths"] == approved_paths
        assert "src/pkg/mod.py" in repaired_event["text"]
        assert "tests/test_mod.py" in repaired_event["text"]
        assert "1 stale pin(s)" in repaired_event["text"]

    assert "commit_refused" in kinds, kinds
    # The checkpoint's own refusal must not be silently swallowed either —
    # and (this ticket) a checkpoint is a safety net, not a publish gate: an
    # unrepairable refusal on a `[WIP-*]` commit is bypassed (`--no-verify`)
    # rather than dropping the WIP, so this is `checkpoint_unverified`, not
    # `checkpoint_failed`.
    assert "checkpoint_unverified" in kinds, kinds

    attempts = await store.list_attempts(t.id)
    assert attempts, "no attempt was recorded"
    for a in attempts:
        assert a["status"] == "failed", a
        assert "REFUSED" in a["failure_reason"], a["failure_reason"]


# --------------------------------------------------------------------------- #
# _checkpoint_wip / _raise_blocker unit-level coverage (the ticket under
# test): the WIP-BLOCKED checkpoint used to bypass the manifest-repair seam
# entirely (`repo.commit_all` called directly), so 9/9 checkpoints were lost
# on the live server whenever a repairable gate refusal hit. These drive
# `_checkpoint_wip`/`_raise_blocker` directly against a real `GitRepo` on
# `bare_repo`, with the seam itself mocked the same way the tests above do.
# --------------------------------------------------------------------------- #

async def test_wip_checkpoint_survives_a_repairable_manifest_refusal(
        bare_repo, tmp_path, store, monkeypatch):
    """Criteria #1, #5: `_checkpoint_wip` must route through
    `commit_with_manifest_repair` with `paths=None` — the exact bug this
    ticket fixes. Mutation proof: reverting `_checkpoint_wip` to call
    `repo.commit_all` directly leaves `calls` empty and fails the first
    assertion below."""
    from no_human.core import orchestrator as orch_mod

    repo = GitRepo(bare_repo)
    repo.create_branch("wip/checkpoint-survives")
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")

    calls = []

    def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg, on_repair=None):
        calls.append((edited, commit_msg))
        return repo_arg.commit_all(commit_msg)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("checkpoint test", repo_path=str(bare_repo))
    await store.create_task(t)

    sha = orch._checkpoint_wip(repo, t)

    assert calls, ("commit_with_manifest_repair was never called — the "
                   "checkpoint bypassed the repair seam")
    edited, commit_msg = calls[0]
    assert edited is None, f"paths must be None (whole-tree WIP commit), got {edited!r}"
    assert commit_msg.startswith("[WIP-BLOCKED] "), commit_msg
    assert sha, "checkpoint must return a non-empty resume sha"
    assert _SHA_RE.match(sha), sha
    assert [e["kind"] for e in events].count("checkpoint") == 1


async def test_a_checkpoint_repair_is_on_the_task_record(
        bare_repo, tmp_path, store, monkeypatch):
    """Criterion #2: when the seam repairs a stale pin before committing
    successfully, the checkpoint path must emit `manifest_repaired` — same
    event shape as the normal commit path (orchestrator.py:4733)."""
    from no_human.core import orchestrator as orch_mod

    repo = GitRepo(bare_repo)
    repo.create_branch("wip/checkpoint-repair-recorded")
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")

    def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg, on_repair=None):
        if on_repair is not None:
            on_repair(["src/pkg/mod.py"], "1 stale pin(s)")
        return repo_arg.commit_all(commit_msg)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("checkpoint repair test", repo_path=str(bare_repo))
    await store.create_task(t)

    sha = orch._checkpoint_wip(repo, t)

    assert sha, "the repaired commit must still produce a resume sha"
    kinds = [e["kind"] for e in events]
    assert kinds.count("manifest_repaired") == 1, kinds
    repaired = [e for e in events if e["kind"] == "manifest_repaired"][0]
    assert repaired["paths"] == ["src/pkg/mod.py"]
    assert "src/pkg/mod.py" in repaired["text"]


async def test_every_manifest_repair_note_reaches_the_event(
        bare_repo, tmp_path, store, monkeypatch):
    """AC4: a single `commit_with_manifest_repair` call can invoke
    `on_repair` more than once in one drain -- e.g. a proactive count-drift
    reconciliation (rewrites EXPORT_CLASSIFICATION.txt) followed by the
    ordinary pin re-approve in the SAME call. `_emit_manifest_repairs` must
    union every note and every path into the one `manifest_repaired` event,
    not only `repaired[0]` -- the second note (and its path) must not be
    silently shadowed."""
    from no_human.core import orchestrator as orch_mod

    repo = GitRepo(bare_repo)
    repo.create_branch("wip/every-repair-note")
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return a + b + 3\n")

    def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg, on_repair=None):
        if on_repair is not None:
            on_repair(["a.py"], "pre-commit pin maintenance: pinned a.py")
            on_repair(["b.py"], "count drift reconciled: drop tests/**: 1 -> 2")
        return repo_arg.commit_all(commit_msg)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("multi-note repair test", repo_path=str(bare_repo))
    await store.create_task(t)

    sha = orch._checkpoint_wip(repo, t)

    assert sha, "the repaired commit must still produce a resume sha"
    kinds = [e["kind"] for e in events]
    assert kinds.count("manifest_repaired") == 1, kinds
    repaired = [e for e in events if e["kind"] == "manifest_repaired"][0]
    assert repaired["paths"] == ["a.py", "b.py"], repaired
    assert "pinned a.py" in repaired["text"], repaired
    assert "count drift reconciled" in repaired["text"], repaired
    assert repaired["notes"] == [
        "pre-commit pin maintenance: pinned a.py",
        "count drift reconciled: drop tests/**: 1 -> 2",
    ], repaired


async def test_a_checkpoint_repair_before_a_second_refusal_is_still_recorded(
        bare_repo, tmp_path, store, monkeypatch):
    """Criterion #2 (ledger-never-silent): a repair that happens but is
    followed by a second refusal must still surface `manifest_repaired` —
    the mutation already happened in the working tree, same
    drain-on-every-exit-path rule as the normal commit path's `finally`
    (orchestrator.py:4728).

    POLICY CORRECTED (this ticket): PR #541 pinned `sha == ""` and
    `checkpoint_failed` here, treating an unrepairable second refusal on the
    checkpoint exactly like a lost commit. That was the wrong policy — a
    checkpoint is a safety net, not a publish gate. The pre-commit
    manifest/export gate protects what SHIPS; a `[WIP-*]` checkpoint on a
    task branch ships nothing (review/approve re-run the gate on the FINAL
    tree before anything merges). So when repair still cannot reconcile the
    refusal, `_checkpoint_wip` now commits the WIP anyway with the gate
    bypassed (`GitRepo.commit_all(..., bypass_gate=True)`) and reports
    `checkpoint_unverified` — committed, but unverified — instead of losing
    the work.
    """
    from no_human.core import orchestrator as orch_mod
    from no_human.vcs import GitError

    repo = GitRepo(bare_repo)
    repo.create_branch("wip/checkpoint-repair-then-refused")
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return a + b + 2\n")

    def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg, on_repair=None):
        if on_repair is not None:
            on_repair(["src/pkg/mod.py"], "1 stale pin(s)")
        raise GitError(_MANIFEST_REFUSAL)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("checkpoint repair-then-refuse test", repo_path=str(bare_repo))
    await store.create_task(t)

    sha = orch._checkpoint_wip(repo, t)

    assert _SHA_RE.match(sha), (
        "an unrepairable second refusal on a [WIP-*] checkpoint must still "
        f"be committed (gate bypassed), never lost: got {sha!r}")
    kinds = [e["kind"] for e in events]
    assert kinds.count("manifest_repaired") == 1, kinds
    assert "checkpoint_unverified" in kinds, kinds


async def test_an_unrepairable_checkpoint_refusal_never_crashes_routing(
        bare_repo, tmp_path, store, monkeypatch):
    """Criterion #3: an unparseable refusal, and a `ProtectedBranch`, must
    both be swallowed by `_checkpoint_wip`'s own guarantee — "checkpoint
    must never crash routing" — even though the normal commit path
    re-raises `ProtectedBranch`. That re-raise is deliberately NOT mirrored
    here (out of scope, unchanged): a checkpoint must never crash, full
    stop."""
    from no_human.core import orchestrator as orch_mod
    from no_human.vcs import GitError, ProtectedBranch

    cfg = _config(tmp_path)

    for exc in (GitError("some unrelated hook failure"),
                ProtectedBranch("refusing to commit on protected branch: main")):
        repo = GitRepo(bare_repo)
        repo.create_branch(f"wip/checkpoint-crash-guard-{exc.__class__.__name__}")
        (bare_repo / "calc.py").write_text(
            f"def add(a, b):\n    return a + b  # {exc.__class__.__name__}\n")

        def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg,
                                             on_repair=None, _exc=exc):
            raise _exc

        monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                            fake_commit_with_manifest_repair)

        events = []
        orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                            SlackNotifier(None), event_sink=events.append)
        t = Task.new("checkpoint crash guard test", repo_path=str(bare_repo))
        await store.create_task(t)

        sha = orch._checkpoint_wip(repo, t)  # must not raise

        assert sha == "", f"{exc.__class__.__name__} must not produce a sha"
        assert "checkpoint_failed" in [e["kind"] for e in events]

    # A THIRD case: the refusal IS a gate refusal (bypass is attempted), but
    # the bypass commit itself also fails. This must still fall through to
    # the original loss path (`checkpoint_failed`, sha == ""), naming BOTH
    # the original gate refusal and the bypass failure — never fabricate a
    # sha when neither commit attempt actually succeeded.
    repo = GitRepo(bare_repo)
    repo.create_branch("wip/checkpoint-crash-guard-bypass-also-fails")
    (bare_repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b  # bypass-also-fails\n")

    def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg, on_repair=None):
        raise GitError(_MANIFEST_REFUSAL)

    def fake_commit_all(self, message, *, bypass_gate=False):
        raise GitError("disk full: cannot write commit object")

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)
    monkeypatch.setattr(GitRepo, "commit_all", fake_commit_all)

    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("checkpoint crash guard bypass-also-fails test",
                repo_path=str(bare_repo))
    await store.create_task(t)

    sha = orch._checkpoint_wip(repo, t)  # must not raise

    assert sha == "", "a bypass commit that itself fails must not fabricate a sha"
    kinds = [e["kind"] for e in events]
    assert "checkpoint_failed" in kinds, kinds
    assert "checkpoint_unverified" not in kinds, kinds
    failed_texts = [e["text"] for e in events if e["kind"] == "checkpoint_failed"]
    assert any("REFUSED" in t_ and "disk full" in t_ for t_ in failed_texts), failed_texts


async def test_a_lost_checkpoint_reaches_the_task_not_just_the_log(
        bare_repo, tmp_path, store, monkeypatch):
    """Criterion #4: a checkpoint's outcome must reach the TASK — both an
    event AND the blocker (intake resolution "(c) both") — not just
    `log.warning`. Drives `_raise_blocker` directly with a seam that always
    refuses.

    POLICY CORRECTED (this ticket): PR #541 pinned this as a LOST checkpoint
    (`resume_commit == ""`, "NO resume commit" in evidence) for an
    unrepairable gate refusal. That was the wrong policy — a checkpoint is a
    safety net, not a publish gate: the gate protects what SHIPS, and a
    `[WIP-*]` checkpoint ships nothing. So the WIP is now committed with the
    gate bypassed and `resume_commit` is the real (bypass) sha; what now
    travels to the task is the UNVERIFIED fact — the gate refused, but the
    work was not lost — via `checkpoint_unverified` and a `blocker.evidence`
    note naming the refusal.
    """
    from no_human.blockers import Blocker, BlockerCategory
    from no_human.core import orchestrator as orch_mod
    from no_human.vcs import GitError

    repo = GitRepo(bare_repo)
    repo.create_branch("wip/lost-checkpoint-reaches-task")
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return a + b + 3\n")

    def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg, on_repair=None):
        raise GitError(_MANIFEST_REFUSAL)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("lost checkpoint test", repo_path=str(bare_repo))
    await store.create_task(t)

    blocker = Blocker(
        category=BlockerCategory.NOVEL_UNKNOWN, transient=False, confidence=0.5,
        goal="implement the feature", root_cause_hypothesis="unknown",
    )

    await orch._raise_blocker(t, blocker, repo=repo)

    kinds = [e["kind"] for e in events]
    assert "checkpoint_unverified" in kinds, kinds
    assert "checkpoint_failed" not in kinds, kinds

    done = await store.get_task(t.id)
    assert done.blocker is not None
    assert _SHA_RE.match(done.blocker["resume_commit"] or ""), done.blocker
    assert "REFUSED" in done.blocker["evidence"], done.blocker["evidence"]
    assert "NO resume commit" not in done.blocker["evidence"], done.blocker["evidence"]


async def test_a_gate_refused_checkpoint_is_committed_unverified_not_lost(
        bare_repo, tmp_path, store, monkeypatch):
    """AC1 (this ticket): "A checkpoint is a safety net, not a publish gate:
    when manifest repair cannot make the pre-commit gate pass, the WIP must
    still be committed (with the refusal recorded), never dropped."

    Real `GitRepo` on `bare_repo`; only `commit_with_manifest_repair` is
    mocked to raise the gate's own refusal shape — `_checkpoint_wip` itself
    must fall back to `repo.commit_all(msg, bypass_gate=True)` and actually
    create the commit. Fails on unfixed code: today `resume_commit == ""`
    and the dirty file is never committed at all.
    """
    from no_human.blockers import Blocker, BlockerCategory
    from no_human.core import orchestrator as orch_mod
    from no_human.vcs import GitError

    repo = GitRepo(bare_repo)
    repo.create_branch("wip/gate-refused-checkpoint-committed")
    (bare_repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 4  # gate-refused checkpoint\n")

    def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg, on_repair=None):
        raise GitError(_MANIFEST_REFUSAL)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("gate-refused checkpoint test", repo_path=str(bare_repo))
    await store.create_task(t)

    blocker = Blocker(
        category=BlockerCategory.NOVEL_UNKNOWN, transient=False, confidence=0.5,
        goal="implement the feature", root_cause_hypothesis="unknown",
    )

    await orch._raise_blocker(t, blocker, repo=repo)

    done = await store.get_task(t.id)
    assert done.blocker is not None
    sha = done.blocker["resume_commit"]
    assert _SHA_RE.match(sha or ""), (
        f"the WIP must still be committed even though the gate refused: {done.blocker!r}")

    # The sha is real HEAD, and the commit is the [WIP-BLOCKED] checkpoint —
    # not some unrelated commit that happened to exist already.
    assert repo.head_sha() == sha
    subject = _git_out(bare_repo, "log", "-1", "--format=%s", sha)
    assert subject.startswith("[WIP-BLOCKED] "), subject

    kinds = [e["kind"] for e in events]
    assert "checkpoint_unverified" in kinds, kinds
    unverified_events = [e for e in events if e["kind"] == "checkpoint_unverified"]
    assert any("REFUSED" in e.get("refusal", "") for e in unverified_events), unverified_events

    assert "REFUSED" in done.blocker["evidence"], done.blocker["evidence"]
    assert "NO resume commit" not in done.blocker["evidence"], done.blocker["evidence"]


async def test_a_clean_park_returns_head_and_creates_no_commit(
        bare_repo, tmp_path, store, monkeypatch):
    """Criterion #6: negative control — a park with no uncommitted changes
    must return `repo.head_sha()` and create no empty commit; the repair
    seam must never even be called."""
    from no_human.core import orchestrator as orch_mod

    repo = GitRepo(bare_repo)
    repo.create_branch("wip/clean-park-no-commit")
    before_sha = repo.head_sha()
    before_count = _git_out(bare_repo, "rev-list", "--count", "HEAD")

    calls = []

    def fake_commit_with_manifest_repair(repo_arg, edited, commit_msg, on_repair=None):
        calls.append((edited, commit_msg))
        return repo_arg.commit_all(commit_msg)

    monkeypatch.setattr(orch_mod, "commit_with_manifest_repair",
                        fake_commit_with_manifest_repair)

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(lambda cwd: None),
                        SlackNotifier(None), event_sink=events.append)
    t = Task.new("clean park test", repo_path=str(bare_repo))
    await store.create_task(t)

    sha = orch._checkpoint_wip(repo, t)

    assert sha == before_sha == repo.head_sha()
    after_count = _git_out(bare_repo, "rev-list", "--count", "HEAD")
    assert after_count == before_count, "a clean park must create no commit"
    assert not calls, "the repair seam must never be called on a clean tree"
    assert "checkpoint" not in [e["kind"] for e in events]
