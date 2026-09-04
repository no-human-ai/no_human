"""Liveness diagnostics: the silences must be enumerable.

Every contradiction rule in doctor.py is a silent death the project really
had; these tests pin each one to a synthetic DB that reproduces it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from no_human import config as cfgmod
from no_human.agent import codex_backend as _cx
from no_human.core.db import Store
from no_human.core.task import Task, TaskStatus
from no_human.doctor import MECHANISMS, codex_row, diagnose

from tests.test_codex_backend import _MODERN_HELP_TEXT, _MODERN_RESUME_HELP_TEXT


def _ev(kind: str, **extra) -> dict:
    return {"source": "test", "kind": kind, "text": "", "ts": time.time(), **extra}


async def test_empty_db_reports_all_mechanisms_as_never_fired(store):
    d = await diagnose(store)
    assert len(d.mechanisms) == len(MECHANISMS)
    assert all(m["count"] == 0 and m["hint"] for m in d.mechanisms)
    assert d.healthy, "an empty install has nothing to contradict"


async def test_the_testing_dead_pattern_is_a_contradiction(store):
    """Reviews ran while tests never did — unnoticed for the system's life."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("review"), _ev("review")])
    d = await diagnose(store)
    assert any("TESTS NEVER RAN" in c for c in d.contradictions)
    assert not d.healthy


async def test_stale_eval_sandbox_is_an_advisory_not_a_contradiction(store):
    """0.4: a leaked eval sandbox is surfaced as an advisory — it must inform
    without failing the doctor gate (healthy stays True)."""
    import os
    import shutil
    import tempfile
    from pathlib import Path

    sandbox = Path(tempfile.gettempdir()) / f"nh-eval-doctortest-{os.getpid()}"
    sandbox.mkdir(exist_ok=True)
    old = time.time() - 3 * 3600  # older than the 2h staleness cutoff
    os.utime(sandbox, (old, old))
    try:
        d = await diagnose(store)
        assert any(str(sandbox) in a for a in d.advisories)
        assert not any(str(sandbox) in c for c in d.contradictions)
        assert d.healthy, "an advisory must never fail the doctor gate"
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


async def test_the_silent_watcher_pattern_is_a_contradiction(store):
    """A task parked at awaiting_approval with zero watcher events ever."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("pr_open"), _ev("review"), _ev("tests")])
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    d = await diagnose(store)
    assert any("WATCHER SILENT" in c for c in d.contradictions)
    # One fresh persisted watcher event clears it.
    await store.save_events(t.id, [_ev("pr_feedback_skipped", source="watcher")])
    d = await diagnose(store)
    assert not any("WATCHER" in c for c in d.contradictions)


async def test_stale_watcher_evidence_is_a_contradiction(store):
    """Heartbeats are hourly; a parked task whose newest watcher evidence is
    hours old means the watcher stopped ticking after it last acted."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("pr_open"), _ev("review"), _ev("tests"),
        {**_ev("wake_tick", source="watcher"), "ts": time.time() - 10 * 3600},
    ])
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    d = await diagnose(store)
    assert any("WATCHER STALE" in c for c in d.contradictions)


async def test_a_status_without_its_evidence_is_a_gap(store):
    """awaiting_approval with no pr_open event = a signal that lies."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
    d = await diagnose(store)
    assert any(t.id[:8] in g and "pr_open" in g for g in d.evidence_gaps)
    await store.save_events(t.id, [_ev("pr_open")])
    d = await diagnose(store)
    assert not d.evidence_gaps


async def test_an_escalation_with_an_empty_blocker_is_a_gap(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.set_status(t, TaskStatus.ESCALATED, validate=False)
    d = await diagnose(store)
    assert any("empty blocker" in g for g in d.evidence_gaps)
    t.blocker = {"question": "Spend more, or stop here?"}
    await store.update_task(t)
    d = await diagnose(store)
    assert not any("empty blocker" in g for g in d.evidence_gaps)


async def test_unreviewed_pr_is_a_contradiction(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("pr_open"), _ev("tests")])
    d = await diagnose(store)
    assert any("UNREVIEWED" in c for c in d.contradictions)


async def test_ci_gate_triggered_but_never_passed_on_a_done_task_contradicts(store):
    """M6: a done task whose CI_GATE integration run started and never went
    green is a verdict without its evidence."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("pr_open"), _ev("review"), _ev("tests"),
        _ev("ci_gate_trigger"),
    ])
    await store.set_status(t, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    d = await diagnose(store)
    assert any("CI_GATE UNPROVEN" in c for c in d.contradictions)
    # The pass event clears it.
    await store.save_events(t.id, [_ev("ci_gate_pass")])
    d = await diagnose(store)
    assert not any("CI_GATE UNPROVEN" in c for c in d.contradictions)


async def test_ci_gate_integration_is_an_enumerated_mechanism(store):
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [_ev("ci_gate_trigger"), _ev("ci_gate_pass")])
    d = await diagnose(store)
    m = next(m for m in d.mechanisms if m["name"] == "ci_gate_integration")
    assert m["count"] == 2


async def test_spurious_budget_escalation_after_ci_gate_pass_contradicts(store):
    """The 2026-07-10 shape: validation passed, no new coder work, yet the
    task sits escalated BUDGET_EXHAUSTED — a resume fired on a non-human
    trigger."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("pr_open"), _ev("attempt_start"), _ev("ci_gate_pass"),
    ])
    t.blocker = {"category": "BUDGET_EXHAUSTED", "question": "raise?"}
    await store.update_task(t)
    await store.set_status(t, TaskStatus.ESCALATED, validate=False)
    d = await diagnose(store)
    assert any("SPURIOUS ESCALATION" in c for c in d.contradictions)
    # Real coder work AFTER the pass = a legitimate escalation — no flag.
    await store.save_events(t.id, [_ev("attempt_start")])
    d = await diagnose(store)
    assert not any("SPURIOUS ESCALATION" in c for c in d.contradictions)


async def test_orphaned_worktree_is_a_contradiction(store, tmp_path, monkeypatch):
    """W2.6: a crashed run's worktree lingers invisibly until the next acquire
    fails or the disk fills. A worktree whose task is KNOWN to this store but
    inactive (failed/done) is an orphan; one owned by a running task is not;
    one whose id is unknown to this store belongs to a different install and
    must NOT be flagged (that false positive broke the empty-DB doctor test)."""
    fake_home = tmp_path / ".no_human"
    (fake_home / "worktrees" / "deadbeef1234").mkdir(parents=True)
    monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", fake_home)

    # Unknown to this store → NOT flagged (different install / isolated test).
    d = await diagnose(store)
    assert not any("ORPHANED WORKTREE" in c for c in d.contradictions)

    # A known but FAILED task with a lingering worktree → orphan.
    t = Task.new("crashed", repo_path="/tmp/x")
    t.id = "deadbeef1234"
    await store.create_task(t)
    await store.set_status(t, TaskStatus.FAILED, validate=False)
    d = await diagnose(store)
    assert any("ORPHANED WORKTREE" in c and "deadbeef1234" in c
               for c in d.contradictions)

    # The same worktree owned by an actively-implementing task: not an orphan.
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    d = await diagnose(store)
    assert not any("ORPHANED WORKTREE" in c for c in d.contradictions)


async def test_orphaned_worktree_is_found_under_the_per_run_name(
    store, tmp_path, monkeypatch,
):
    """Worktree directories are named `<task_id>.<owner_pid>.<token>` — one per
    RUN, so two overlapping attempts of a task cannot share a checkout. The
    orphan check has to READ that name: matching the directory against task ids
    whole would simply have stopped finding anything, with no test failing.

    It also gains a signal the per-task shape could not carry. An ACTIVE task
    can own a leftover — killed run, live run, both on disk — and a dead owner
    pid says which is which without guessing.
    """
    fake_home = tmp_path / ".no_human"
    wt = fake_home / "worktrees"
    dead_owner = wt / "deadbeef1234.4194303.a1b2c3d4"
    dead_owner.mkdir(parents=True)
    monkeypatch.setattr("no_human.config.NO_HUMAN_HOME", fake_home)

    t = Task.new("crashed mid-run", repo_path="/tmp/x")
    t.id = "deadbeef1234"
    await store.create_task(t)

    # ACTIVE task, but this directory's owner process is gone: still an orphan.
    await store.set_status(t, TaskStatus.IMPLEMENTING, validate=False)
    d = await diagnose(store)
    assert any("ORPHANED WORKTREE" in c and str(dead_owner) in c
               for c in d.contradictions), "the per-run name was not attributed"
    assert any("owner process 4194303 is gone" in c for c in d.contradictions)

    # The SAME task's live directory — owner alive — is not an orphan.
    import os

    live = wt / f"deadbeef1234.{os.getpid()}.b2c3d4e5"
    live.mkdir()
    d = await diagnose(store)
    assert not any(str(live) in c for c in d.contradictions)


async def test_done_code_review_needs_no_pr_open(store):
    """A standalone code-review finishes with cited comments, not a PR — 'done'
    without pr_open must NOT be flagged as an evidence gap for it (false positive
    that flagged f71107e9 every run). A done FEATURE task still must have one."""
    cr = Task.new("review PR 123", repo_path="/tmp/x")
    cr.kind = "code_review"
    await store.create_task(cr)
    await store.set_status(cr, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    d = await diagnose(store)
    assert not any(cr.id[:8] in g and "pr_open" in g for g in d.evidence_gaps)

    feat = Task.new("add feature", repo_path="/tmp/x")
    feat.kind = "feature"
    await store.create_task(feat)
    await store.set_status(feat, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    d = await diagnose(store)
    assert any(feat.id[:8] in g and "pr_open" in g for g in d.evidence_gaps)


async def test_doctor_flags_failed_attempts_with_empty_reason(tmp_path):
    """Historical rows (and any path that bypasses the store backstop) must
    surface as an evidence gap, not stay invisible."""
    from no_human.core.db import Store
    from no_human.core.task import Task
    from no_human.doctor import diagnose

    store = await Store(tmp_path / "d.db").connect()
    try:
        t = Task.new("x", repo_path="/tmp/r")
        await store.create_task(t)
        a = await store.create_attempt(t.id, 1)
        # bypass the backstop deliberately (simulates a historical row)
        await store.db.execute(
            "UPDATE attempts SET status='failed', failure_reason=NULL WHERE id=?",
            (a,))
        await store.db.commit()
        d = await diagnose(store)
        assert any("failure_reason" in g for g in d.evidence_gaps), d.evidence_gaps
    finally:
        await store.close()


async def test_doctor_accepts_report_only_design_doc_as_done(tmp_path):
    """design_doc joins PR_LESS_KINDS: done-without-PR is its success shape
    (this fix was silently dropped from PR #29 — pinned this time)."""
    from no_human.core.db import Store
    from no_human.core.task import Task, TaskStatus
    from no_human.doctor import diagnose

    store = await Store(tmp_path / "d.db").connect()
    try:
        t = Task.new("design doc", repo_path="/tmp/r", kind="design_doc")
        await store.create_task(t)
        await store.db.execute("UPDATE tasks SET status='done' WHERE id=?", (t.id,))
        await store.db.commit()
        d = await diagnose(store)
        assert not any(t.id[:8] in g for g in d.evidence_gaps), d.evidence_gaps
    finally:
        await store.close()


def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _git_out(repo_path, *args):
    return subprocess.run(["git", "-C", str(repo_path), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


async def test_landed_override_row_reports_no_evidence_gap(tmp_path, store):
    """AC5: a task rescued via `approve_landed_override`'s stacked-base
    candidate widening (the task's recorded base_branch names another
    task's stacked branch, but content lands on the repo's real default)
    records real evidence — `approved_landed_override` is already in
    `DONE_EVIDENCE_KINDS` — so `nh doctor`'s evidence-gap check must not
    flag the repaired row. Non-vacuity control: a DONE task with none of
    DONE_EVIDENCE_KINDS on record IS still reported, so this isn't a check
    that can't fail."""
    from no_human.blockers.landed_override import approve_landed_override

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "no-human/parent-3")
    (repo / "stacked.txt").write_text("stacked base work\n")
    _git(repo, "add", "stacked.txt")
    _git(repo, "commit", "-m", "parent task: stacked work")
    _git(repo, "checkout", "main")
    (repo / "a.txt").write_text("actual landed change\n")
    _git(repo, "commit", "-am", "landed: the real fix")
    landed_sha = _git_out(repo, "rev-parse", "main")
    default_sha = _git_out(repo, "rev-parse", "main")
    _git(repo, "update-ref", "refs/remotes/origin/main", default_sha)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD",
         "refs/remotes/origin/main")

    t = Task.new("rescue", repo_path=str(repo))
    t.context = {"base_branch": "no-human/parent-3", "pr_branch": ""}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)

    await approve_landed_override(
        store, t, landed_sha,
        "landed on main; base_branch recorded the parent task's stacked "
        "branch by dispatch-time accident")

    d = await diagnose(store)
    assert not any(t.id[:8] in g for g in d.evidence_gaps), d.evidence_gaps

    # non-vacuity control: a DONE task with NO DONE_EVIDENCE_KINDS event on
    # record is still reported — proves the check above could have failed.
    other = Task.new("no evidence", repo_path=str(repo), kind="feature")
    await store.create_task(other)
    await store.set_status(other, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})
    d2 = await diagnose(store)
    assert any(other.id[:8] in g for g in d2.evidence_gaps), d2.evidence_gaps


async def test_done_no_evidence_row_is_repairable_and_stops_being_a_gap(
    tmp_path, store,
):
    """AC1 (doctor half), nh67: a DONE task from before the completion-
    evidence mechanism existed (hand-landed, none of DONE_EVIDENCE_KINDS on
    record) must stop being an evidence gap once `approve_landed_override`'s
    `done_no_evidence` shape repairs it — proven via `diagnose(store)` itself,
    not by asserting the event was written. Non-vacuity control: a second,
    untouched DONE-no-evidence row is still reported after the repair."""
    from no_human.blockers.landed_override import approve_landed_override

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    landed_sha = _git_out(repo, "rev-parse", "main")

    t1 = Task.new("hand-landed-1", repo_path=str(repo), kind="feature")
    t1.context = {"base_branch": "main", "pr_branch": ""}
    await store.create_task(t1)
    await store.set_status(t1, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})

    t2 = Task.new("hand-landed-2", repo_path=str(repo), kind="feature")
    t2.context = {"base_branch": "main", "pr_branch": ""}
    await store.create_task(t2)
    await store.set_status(t2, TaskStatus.DONE, validate=False,
                           event={"source": "test", "kind": "test_seed"})

    d1 = await diagnose(store)
    assert any(t1.id[:8] in g for g in d1.evidence_gaps), d1.evidence_gaps
    assert any(t2.id[:8] in g for g in d1.evidence_gaps), d1.evidence_gaps

    await approve_landed_override(
        store, t1, landed_sha,
        "repairing a pre-mechanism hand-landing with no completion evidence")

    d2 = await diagnose(store)
    assert not any(t1.id[:8] in g for g in d2.evidence_gaps), d2.evidence_gaps
    assert any(t2.id[:8] in g for g in d2.evidence_gaps), d2.evidence_gaps


# --------------------------------------------------------------------------- #
# Configured-but-unusable CI. Not a history check: this failure mode leaves no #
# events at all, so no amount of event counting could ever have found it.      #
# --------------------------------------------------------------------------- #

async def test_ci_enabled_but_targetless_is_a_contradiction(store):
    d = await diagnose(store, {"ci": {"enabled": True, "backend": "gitlab",
                                      "project": ""}})
    assert any("CI BACKEND UNUSABLE" in c for c in d.contradictions)
    assert not d.healthy, "a gate the operator believes in but does not have"


@pytest.mark.parametrize("backend,key", [
    ("gitlab", "ci.project"),
    ("github_actions", "ci.repo"),
    ("jenkins", "ci.job"),
])
async def test_ci_contradiction_names_the_key_to_set(store, backend, key):
    """`nh doctor` is the surface a user checks when they suspect this, so the
    line has to end their search, not start it. It used to say "project/repo/job
    are all empty" for every backend — true, and it leaves the user to work out
    which one THEIR backend needs."""
    d = await diagnose(store, {"ci": {"enabled": True, "backend": backend}})
    assert d.contradictions
    assert any(key in c for c in d.contradictions), d.contradictions


async def test_ci_unknown_backend_is_a_contradiction(store):
    d = await diagnose(store, {"ci": {"enabled": True, "backend": "travis",
                                      "project": "g/r"}})
    # Asserts the MESSAGE, not the exception class name: `unknown ci.backend`
    # is what the operator reads, and CIMisconfigured (a ValueError) now
    # carries it. A diagnostic that leaked "ValueError" told them nothing.
    assert any("unknown ci.backend" in c for c in d.contradictions)
    assert any("travis" in c for c in d.contradictions)


async def test_working_ci_config_is_not_flagged(store):
    d = await diagnose(store, {"ci": {"enabled": True, "backend": "gitlab",
                                      "project": "g/r"}})
    assert not any("CI BACKEND" in c for c in d.contradictions)
    assert d.healthy


async def test_shipped_default_ci_config_is_silent(store):
    """Devil's advocate: doctor must stay green for an install that never
    configured CI. Read from DEFAULT_CONFIG — load_config() deep-merges the
    operator's own ~/.no_human/config.yaml, so asserting on a loaded config
    would prove something about this machine, not about the product.
    """
    from no_human.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["ci"]["enabled"] is False
    d = await diagnose(store, DEFAULT_CONFIG)
    assert not any("CI BACKEND" in c for c in d.contradictions)
    assert d.healthy


async def test_diagnose_without_config_is_unchanged(store):
    """26 existing callers pass only the store — they must keep working."""
    d = await diagnose(store)
    assert d.healthy


# --------------------------------------------------------------------------- #
# Codex coding-backend readiness (`doctor.codex_readiness`, wired into         #
# `diagnose`). The row/contradictions are only produced when the codex        #
# backend is actually in play — everything below either proves that gate or   #
# probes what the row says once it fires. CLI probes are stubbed via the same #
# `_stub_cli`-style helper `tests/test_codex_backend.py` already established, #
# so nothing here spawns a real subprocess or reaches OpenAI.                 #
# --------------------------------------------------------------------------- #

_INCOMPATIBLE_HELP_TEXT = (
    "codex-exec\n\nUSAGE:\n    codex exec [OPTIONS] [PROMPT]\n\nOPTIONS:\n"
    "    --json                       Print events as JSONL\n"
    "    --cd <DIR>                   Set the working directory\n"
    "    --model <MODEL>              Model to use\n"
    "    --sandbox <MODE>             read-only | workspace-write | danger-full-access\n"
)

#: A `codex exec resume --help` shape that is otherwise modern (still has
#: `--config`, so `approval_args` succeeds) but is missing `--model` — a
#: narrower, resume-only incompatibility distinct from the approval-mode
#: absence `_INCOMPATIBLE_HELP_TEXT` exercises above.
_INCOMPATIBLE_RESUME_HELP_TEXT = (
    "codex-exec-resume\n\nUSAGE:\n    codex exec resume [OPTIONS] [THREAD_ID]\n\n"
    "OPTIONS:\n"
    "    --json                       Print events as JSONL\n"
    "    -c, --config <key=value>     Override a config value\n"
)


async def test_the_codex_row_is_absent_on_a_default_claude_install(
        store, monkeypatch, isolated_env_file):
    """The default install never even asks — no CLI probe, no key check."""
    def _boom(*_a, **_k):
        raise AssertionError("codex probe ran on an install that never asked for it")

    monkeypatch.setattr(_cx, "find_codex_cli", _boom)
    monkeypatch.setattr(_cx, "codex_exec_help", _boom)
    monkeypatch.setattr(_cx, "codex_version", _boom)

    d = await diagnose(store)  # no config ⇒ worker.backend defaults to claude
    assert d.codex == {"selected": False}
    assert not any("CODEX" in c for c in d.contradictions), d.contradictions
    assert d.healthy


async def test_the_codex_row_reports_version_flags_and_key_presence_never_the_value(
        store, monkeypatch, isolated_env_file):
    monkeypatch.setattr(_cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(
        _cx, "codex_exec_help",
        lambda path, resume=False, timeout=10.0: (
            _MODERN_RESUME_HELP_TEXT if resume else _MODERN_HELP_TEXT
        ),
    )
    monkeypatch.setattr(_cx, "codex_version",
                        lambda path, timeout=10.0: "codex-cli 0.149.0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secretvalue")

    d = await diagnose(store, {"worker": {"backend": "codex"}})

    assert d.codex["selected"] is True
    assert d.codex["version"] == "codex-cli 0.149.0"
    assert d.codex["flags_ok"] is True
    assert d.codex["api_key_present"] is True
    assert d.codex["entitlement_note"]
    assert "sk-secretvalue" not in repr(d.codex)
    assert not any("sk-secretvalue" in c for c in d.contradictions)
    assert not any("sk-secretvalue" in a for a in d.advisories)


async def test_an_incompatible_codex_cli_is_a_contradiction_with_the_version(
        store, monkeypatch, isolated_env_file):
    monkeypatch.setattr(_cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(
        _cx, "codex_exec_help",
        lambda path, resume=False, timeout=10.0: (
            _MODERN_RESUME_HELP_TEXT if resume else _INCOMPATIBLE_HELP_TEXT
        ),
    )
    monkeypatch.setattr(_cx, "codex_version",
                        lambda path, timeout=10.0: "codex-cli 0.99.0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secretvalue")

    d = await diagnose(store, {"worker": {"backend": "codex"}})

    assert d.codex["flags_ok"] is False
    assert any("CODEX CLI INCOMPATIBLE" in c and "codex-cli 0.99.0" in c
               for c in d.contradictions), d.contradictions
    assert not d.healthy


async def test_an_incompatible_resume_argv_is_also_a_contradiction(
        store, monkeypatch, isolated_env_file):
    """The resume branch has its own, narrower flag surface (no `--cd`, no
    `--sandbox` — verified live). A CLI that accepts the non-resume argv can
    still reject the resume one; this is this ticket's Blocker 1, and the
    doctor check must catch it independently of the non-resume check above."""
    monkeypatch.setattr(_cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(
        _cx, "codex_exec_help",
        lambda path, resume=False, timeout=10.0: (
            _INCOMPATIBLE_RESUME_HELP_TEXT if resume else _MODERN_HELP_TEXT
        ),
    )
    monkeypatch.setattr(_cx, "codex_version",
                        lambda path, timeout=10.0: "codex-cli 0.99.0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secretvalue")

    d = await diagnose(store, {"worker": {"backend": "codex"}})

    # The non-resume argv is fully compatible with `_MODERN_HELP_TEXT`, so
    # `flags_ok` only goes False because the resume argv was also checked.
    assert d.codex["flags_ok"] is False
    assert any("resume:" in c for c in d.contradictions), d.contradictions
    assert not d.healthy


async def test_a_task_that_asks_for_codex_turns_the_row_on(
        store, monkeypatch, isolated_env_file):
    """The global config stays claude — a per-task override is enough."""
    monkeypatch.setattr(_cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(
        _cx, "codex_exec_help",
        lambda path, resume=False, timeout=10.0: (
            _MODERN_RESUME_HELP_TEXT if resume else _MODERN_HELP_TEXT
        ),
    )
    monkeypatch.setattr(_cx, "codex_version",
                        lambda path, timeout=10.0: "codex-cli 0.149.0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secretvalue")

    t = Task.new("x", repo_path="/tmp/x")
    t.config = {"backend": "codex"}
    await store.create_task(t)

    d = await diagnose(store)  # global config unset ⇒ claude
    assert d.codex["selected"] is True


async def test_a_missing_openai_key_is_a_contradiction_with_a_fix_command(
        store, monkeypatch, isolated_env_file):
    monkeypatch.setattr(_cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(
        _cx, "codex_exec_help",
        lambda path, resume=False, timeout=10.0: (
            _MODERN_RESUME_HELP_TEXT if resume else _MODERN_HELP_TEXT
        ),
    )
    monkeypatch.setattr(_cx, "codex_version",
                        lambda path, timeout=10.0: "codex-cli 0.149.0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    d = await diagnose(store, {"worker": {"backend": "codex"}})

    assert d.codex["api_key_present"] is False
    assert any("~/.no_human/.env" in c for c in d.contradictions), d.contradictions
    assert not d.healthy


async def test_subscription_mode_with_a_live_session_is_healthy_not_a_contradiction(
        store, monkeypatch, isolated_env_file):
    """The exact defect the audit reproduced live: `mode: subscription` + a
    live codex CLI + no OPENAI_API_KEY on file used to be reported as
    'CODEX BACKEND UNUSABLE: no OPENAI_API_KEY on file — codex is BYO-API-key
    only', flipping `d.healthy` (and `nh doctor`'s exit code) on every
    correctly-configured subscription install. `codex_readiness` must be
    mode-aware: subscription mode checks the ChatGPT session instead of a
    key that mode never has."""
    monkeypatch.setattr(_cx, "find_codex_cli", lambda explicit=None: "/bin/codex")
    monkeypatch.setattr(
        _cx, "codex_exec_help",
        lambda path, resume=False, timeout=10.0: (
            _MODERN_RESUME_HELP_TEXT if resume else _MODERN_HELP_TEXT
        ),
    )
    monkeypatch.setattr(_cx, "codex_version",
                        lambda path, timeout=10.0: "codex-cli 0.149.0")
    monkeypatch.setattr(_cx, "codex_login_status",
                        lambda cli_path=None: _cx.CodexSessionStatus(True, "chatgpt"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    d = await diagnose(
        store,
        {"worker": {"backend": "codex"}, "llm": {"codex_auth_mode": "subscription"}},
    )

    assert not any(
        "BYO-API-key only" in c for c in d.contradictions
    ), d.contradictions
    assert not any(
        "no OPENAI_API_KEY on file" in c for c in d.contradictions
    ), d.contradictions
    assert d.codex["api_key_present"] is True
    assert d.healthy


async def test_a_codex_readiness_crash_while_selected_is_a_contradiction_not_silence(
        store, monkeypatch, isolated_env_file):
    """Pins the exact silent-failure a prior review flagged at doctor.py:508:
    a crash inside the readiness probe (or the task-lookup query it shares a
    try-block with) must not blank ``d.codex`` back to ``{"selected": False}``
    with nothing logged — when codex IS the selected backend, a broken check
    is itself a contradiction (unhealthy), not a silent "codex isn't in
    play"."""
    import no_human.doctor as doctor_mod

    def _boom(*_args, **_kwargs):
        raise RuntimeError("codex readiness probe exploded")

    monkeypatch.setattr(doctor_mod, "codex_readiness", _boom)

    d = await diagnose(store, {"worker": {"backend": "codex"}})

    assert d.codex is not None
    assert d.codex["selected"] is True
    assert "codex readiness probe exploded" in d.codex.get("error", "")
    assert any(
        "CODEX READINESS CHECK FAILED" in c for c in d.contradictions
    ), d.contradictions
    assert not d.healthy


async def test_a_codex_readiness_crash_while_not_selected_is_an_advisory_not_silence(
        store, monkeypatch, isolated_env_file):
    """Same crash, but codex is not in play at all (default Claude install,
    no task requests it): the crash must still be surfaced — as an advisory,
    since nothing codex-shaped is actually broken for this install — never
    dropped on the floor, and never a contradiction that would flip a
    healthy Claude-only install to unhealthy."""
    import no_human.doctor as doctor_mod

    def _boom(*_args, **_kwargs):
        raise RuntimeError("store schema too old for the codex task query")

    monkeypatch.setattr(doctor_mod, "codex_readiness", _boom)

    d = await diagnose(store)

    assert d.codex is not None
    assert d.codex["selected"] is False
    assert any(
        "store schema too old" in a for a in d.advisories
    ), d.advisories
    assert not any("CODEX" in c for c in d.contradictions), d.contradictions
    assert d.healthy


# --------------------------------------------------------------------------- #
# `nh doctor`'s EXIT CODE — the machine-readable half of the command.          #
#                                                                              #
# It used to be a constant 0: the command printed a red contradiction and told #
# its caller everything was fine, so `nh doctor || exit 1` in a CI job could   #
# never fire and every gate reporting through doctor was invisible to          #
# automation. These run the real CLI in a subprocess, because an in-process    #
# CliRunner would not exercise the process exit code at all — and would read   #
# the operator's REAL ~/.no_human, since NO_HUMAN_HOME is resolved at import.  #
# HOME and TMPDIR are therefore redirected per test.                           #
# --------------------------------------------------------------------------- #

DOCTOR_SRC = Path(__file__).resolve().parent.parent / "src"


def _run_doctor(home: Path, tmpdir: Path, *, config: str | None = None,
                token: bool = True,
                args: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    """Run `nh doctor` against an isolated HOME. Returns the completed process
    so the caller can assert on ``returncode`` directly."""
    (home / ".no_human").mkdir(parents=True, exist_ok=True)
    if config is not None:
        (home / ".no_human" / "config.yaml").write_text(config)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "CLAUDE_", "AWS_"))}
    env.update({"HOME": str(home), "TMPDIR": str(tmpdir),
                "PYTHONPATH": str(DOCTOR_SRC), "NO_COLOR": "1",
                "COLUMNS": "200"})
    if token:
        # Presence-only probe (backend_check never makes a live auth call), so
        # a placeholder is enough and no real credential is ever read here.
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat-not-a-real-token"
    return subprocess.run(
        [sys.executable, "-m", "no_human.cli.commands", "doctor", *args],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(tmpdir),
    )


def test_doctor_exits_nonzero_on_a_contradiction(tmp_path):
    """`nh doctor || exit 1` must actually fire. `ci.enabled: true` with no
    pipeline target is a contradiction the doctor already prints in red."""
    proc = _run_doctor(tmp_path / "home", _mktmp(tmp_path),
                       config="ci:\n  enabled: true\n  backend: gitlab\n")
    assert "CI BACKEND UNUSABLE" in proc.stdout, proc.stdout
    assert proc.returncode != 0, (
        "doctor printed a contradiction and reported success — the exit code "
        f"carries no information:\n{proc.stdout}"
    )
    assert proc.returncode == 1, f"expected 1, got {proc.returncode}"


def test_doctor_exits_zero_when_healthy(tmp_path):
    """The control that proves the fix is not just "always fail": a fresh
    install with a usable backend and no contradictions still exits 0."""
    proc = _run_doctor(tmp_path / "home", _mktmp(tmp_path))
    assert "no contradictions, no evidence gaps" in proc.stdout, proc.stdout
    assert proc.returncode == 0, (
        f"a healthy install must exit 0, got {proc.returncode}:\n{proc.stdout}"
    )


def test_doctor_advisory_alone_does_not_change_the_exit_code(tmp_path):
    """An advisory is informational — a leaked eval sandbox is a disk leak, not
    a broken gate. If advisories flipped the exit code, `nh doctor || exit 1`
    would fire on benign conditions, someone would delete it from their
    pipeline, and the check would protect nothing."""
    tmpdir = _mktmp(tmp_path)
    sandbox = tmpdir / "nh-eval-advisory-only"
    sandbox.mkdir()
    old = time.time() - 3 * 3600  # older than doctor's 2h staleness cutoff
    os.utime(sandbox, (old, old))

    proc = _run_doctor(tmp_path / "home", tmpdir)
    assert "LEAKED EVAL SANDBOX" in proc.stdout, (
        f"the advisory was not even reported:\n{proc.stdout}")
    assert "✗" not in proc.stdout, (  # the contradiction bullet
        f"this fixture must produce an advisory ONLY:\n{proc.stdout}")
    assert "no contradictions, no evidence gaps" in proc.stdout, proc.stdout
    assert proc.returncode == 0, (
        f"an advisory must never fail the doctor gate, got {proc.returncode}:"
        f"\n{proc.stdout}"
    )


def test_doctor_leads_with_a_three_line_verdict(tmp_path):
    """A first run printed 149 lines of all-zero mechanism rows and internal
    history before saying anything a newcomer could act on (walkthrough B6/Q11).
    The first three lines now answer: is it healthy, has anything run, how many
    tasks."""
    proc = _run_doctor(tmp_path / "home", _mktmp(tmp_path))
    head = [line.strip() for line in proc.stdout.splitlines()[:3]]

    assert head[0].startswith("install healthy"), proc.stdout
    assert head[1] == "nothing has run yet", proc.stdout
    assert head[2] == "0 task(s)", proc.stdout


def test_doctor_reports_the_auth_profile_and_mode(tmp_path):
    """quickstart.md promises `nh doctor` "reports your auth profile and mode".
    It loaded both values and printed neither — 149 lines with no occurrence of
    "auth" at all (walkthrough B5/Q4)."""
    proc = _run_doctor(tmp_path / "home", _mktmp(tmp_path),
                       config="llm:\n  auth_profile: enterprise\n"
                              "  auth_mode: subscription\n")
    auth = [ln for ln in proc.stdout.splitlines() if ln.startswith("auth")]

    assert auth, f"no auth line at all:\n{proc.stdout}"
    assert "enterprise" in auth[0], auth
    assert "subscription" in auth[0], auth
    # And it must not overclaim: the probe is presence-only by design
    # (backend_check never spends quota on a live call).
    assert "presence only" in auth[0], auth


def test_the_mechanism_table_is_behind_verbose_and_the_exit_code_is_not(tmp_path):
    """The mechanism table moves behind `--verbose`; the summary line, the
    `healthy` predicate and the exit code do not move at all.

    Row count comes from `MECHANISMS`, not a literal: registering a mechanism
    is a one-line change and used to fail HERE, in a rendering test that has
    nothing to say about it."""
    home, tmpdir = tmp_path / "home", _mktmp(tmp_path)
    quiet = _run_doctor(home, tmpdir)
    loud = _run_doctor(home, tmpdir, args=("--verbose",))

    # Default: the header survives (it is what `nh doctor` renders at all),
    # the per-mechanism rows do not.
    assert "mechanism liveness" in quiet.stdout, quiet.stdout
    assert "last: never" not in quiet.stdout, quiet.stdout
    assert "review_gate" not in quiet.stdout, quiet.stdout
    assert f"0/{len(MECHANISMS)} have ever fired" in quiet.stdout, quiet.stdout

    # --verbose: the whole table, exactly as it always rendered.
    assert "review_gate" in loud.stdout, loud.stdout
    assert loud.stdout.count("last: never") == len(MECHANISMS), loud.stdout
    assert len(loud.stdout.splitlines()) > len(quiet.stdout.splitlines())

    # Same verdict, same exit code either way.
    assert quiet.returncode == loud.returncode == 0, (quiet.stdout, loud.stdout)
    for out in (quiet.stdout, loud.stdout):
        assert "no contradictions, no evidence gaps" in out, out


async def test_a_dead_distiller_is_readable_from_the_three_distill_mechanisms(store):
    """A LIFETIME count cannot show a death: `context_distill` stood at 162 on
    2026-08-10 with its last firing on 2026-07-28, so doctor read "alive" for
    twelve days while the lever was dead, and the resulting `distill_* == 0`
    was misdiagnosed as lost spend.

    The other two kinds are what separate the causes. All three carry
    `last_ts`, so "distilled 162×, last a fortnight ago; skipped 73×, last
    today" is readable off one screen. Deliberately NOT a contradiction rule:
    keying one off lifetime counts would inherit the exact blindness above."""
    names = {n for n, _, _ in MECHANISMS}
    assert {"context_distill", "context_distill_skipped",
            "context_distill_failed"} <= names

    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("context_distill_skipped", chunks=3, largest=879, threshold=2000,
            reason="no_large_chunk"),
        _ev("context_distill_skipped", chunks=4, largest=782, threshold=2000,
            reason="no_large_chunk"),
    ])
    d = await diagnose(store)
    counts = {m["name"]: m["count"] for m in d.mechanisms}
    assert counts["context_distill_skipped"] == 2
    assert counts["context_distill"] == 0
    assert counts["context_distill_failed"] == 0
    # A skip is not a firing: it must never be swept into the liveness count.
    hints = {m["name"]: m["hint"] for m in d.mechanisms}
    assert hints["context_distill"]          # still flagged as never-fired
    assert not hints["context_distill_skipped"]
    # ...and neither is a health failure — doctor reports, the human decides.
    assert d.evidence_gaps == []


async def test_a_distiller_that_only_throws_is_not_read_as_never_consulted(store):
    """The state the skip kind alone could not see. A gather WITH an oversized
    chunk and a backend that raises emits neither a firing nor a skip, so both
    of those counts sit at zero while distillation is being consulted on every
    gather and failing on every call — and the zero-hint on the skip row said
    that shape meant "not being consulted at all". The failure kind is what
    makes that sentence true: its count is the only surviving evidence,
    because the exception is swallowed and the call never bills `distill_*`."""
    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("context_distill_failed", error="RuntimeError", chars_before=4096),
    ])
    d = await diagnose(store)
    counts = {m["name"]: m["count"] for m in d.mechanisms}
    assert counts["context_distill_failed"] == 1
    assert counts["context_distill"] == counts["context_distill_skipped"] == 0
    hints = {m["name"]: m["hint"] for m in d.mechanisms}
    assert not hints["context_distill_failed"]   # non-zero: no zero-hint shown
    # The skip row's zero-hint may only claim "not consulted" for the case
    # where this row is zero too — the reading it now spells out.
    assert "both other context_distill_* rows at zero" in hints[
        "context_distill_skipped"]
    assert d.evidence_gaps == []


def _mktmp(tmp_path: Path) -> Path:
    """A private TMPDIR, so the machine's real /tmp leftovers cannot leak into
    (or out of) a test that asserts on advisories."""
    d = tmp_path / "tmp"
    d.mkdir(exist_ok=True)
    return d


async def test_the_intake_grill_passes_are_listed_mechanisms(store):
    """A claim in evaluator.py said `nh doctor` picked the grill's outcome
    events up "by kind for free". It did not — MECHANISMS is a hardcoded list
    and neither kind was in it, so the events were counted by nothing here and
    a dead grill stayed a dead grill silently. This test is what makes the
    sentence true: break the entry and the claim fails out loud.
    """
    names = {n for n, _, _ in MECHANISMS}
    assert {"grill_questions", "grill_answering"} <= names

    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)
    await store.save_events(t.id, [
        _ev("grill_questions", outcome="parsed_first_try"),
        _ev("grill_answering", outcome="parsed_first_try"),
        _ev("grill_answering", outcome="no_block_after_retry"),
    ])
    d = await diagnose(store)
    counts = {m["name"]: m["count"] for m in d.mechanisms}
    assert counts["grill_questions"] == 1
    assert counts["grill_answering"] == 2
    # A mechanism that HAS fired carries no zero-hint.
    assert all(not m["hint"] for m in d.mechanisms
               if m["name"].startswith("grill_"))


# --------------------------------------------------------------------------- #
# `nh doctor --verify-auth` — the one gap presence-checking cannot close       #
#                                                                              #
# A valid-SHAPED but expired or revoked credential passes every check in this  #
# file and dies at the first task (walkthrough B5). The live call is OPT-IN,   #
# because the rule that no diagnostic spends quota unasked is what makes the   #
# rest of doctor safe to run anywhere. The live call itself is mocked at its   #
# boundary in every test below: no credential, real or otherwise, is used.     #
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, is_error=False, final_text="ok"):
        self.is_error = is_error
        self.final_text = final_text


def _fake_backend(monkeypatch, *, result=None, raises=None, hang=False):
    """Replace ClaudeBackend.run — the boundary where quota would be spent."""
    import no_human.agent.claude_backend as cb_mod

    seen = {"constructed": 0, "kwargs": None}

    class _FakeBackend:
        def __init__(self, **kw):
            seen["constructed"] += 1
            seen["kwargs"] = kw

        async def run(self, prompt, **kw):
            import asyncio

            seen["prompt"] = prompt
            seen["run_kwargs"] = kw
            if raises is not None:
                raise raises
            if hang:
                await asyncio.sleep(30)
            return result or _FakeResult()

    monkeypatch.setattr(cb_mod, "ClaudeBackend", _FakeBackend)
    return seen


@pytest.fixture
def _no_auth_assertion(monkeypatch):
    """Neutralise the credential export so no test reads ~/.no_human/.env."""
    from no_human import config as config_mod

    monkeypatch.setattr(config_mod, "assert_subscription_mode",
                        lambda **kw: None)


async def test_verify_credential_live_returns_nothing_when_the_call_lands(
        monkeypatch, _no_auth_assertion):
    """The verdict is "an authenticated request succeeded", not "the answer was
    correct" — holding a diagnostic hostage to a model's phrasing would fail
    installs that work."""
    from no_human.agent.backend_check import verify_credential_live

    seen = _fake_backend(monkeypatch, result=_FakeResult(final_text="banana"))
    assert await verify_credential_live(model="claude-haiku-4-5") is None
    # …and it is the CHEAP shape: one turn, low effort, readonly.
    assert seen["run_kwargs"]["max_turns"] == 1
    assert seen["run_kwargs"]["effort"] == "low"
    assert seen["kwargs"]["readonly"] is True


async def test_verify_credential_live_reports_a_rejected_credential(
        monkeypatch, _no_auth_assertion):
    from no_human.agent.backend_check import verify_credential_live

    _fake_backend(monkeypatch, result=_FakeResult(
        is_error=True, final_text="API Error: 401 OAuth token is invalid"))
    problem = await verify_credential_live(model="claude-haiku-4-5")
    assert problem is not None
    assert problem[0] == "rejected"
    assert "401" in problem[1]


async def test_verify_credential_live_reports_a_crash_instead_of_raising(
        monkeypatch, _no_auth_assertion):
    """A diagnostic that dies with a traceback tells the operator less than one
    that says what happened."""
    from no_human.agent.backend_check import verify_credential_live

    _fake_backend(monkeypatch, raises=RuntimeError("CLI not found"))
    problem = await verify_credential_live(model="claude-haiku-4-5")
    assert problem == ("rejected", "RuntimeError: CLI not found")


async def test_a_slow_link_is_not_reported_as_a_dead_credential(
        monkeypatch, _no_auth_assertion):
    """Reporting a timeout as a rejected token would send the operator off to
    regenerate a credential that was fine."""
    from no_human.agent.backend_check import verify_credential_live

    _fake_backend(monkeypatch, hang=True)
    problem = await verify_credential_live(model="claude-haiku-4-5",
                                           timeout_s=0.05)
    assert problem is not None
    assert problem[0] == "inconclusive"
    assert "nothing about the credential" in problem[1]


async def test_a_dead_network_is_not_reported_as_a_dead_credential(
        monkeypatch, _no_auth_assertion):
    """The independent review demonstrated the first cut folding
    OSError("Network is unreachable") into CREDENTIAL DOES NOT WORK — a cron
    doctor on a flaky link must not send the operator to rotate a credential
    the API never even saw."""
    from no_human.agent.backend_check import verify_credential_live

    _fake_backend(monkeypatch, raises=OSError("Network is unreachable"))
    problem = await verify_credential_live(model="claude-haiku-4-5")
    assert problem is not None
    assert problem[0] == "inconclusive"
    assert "never reached the API" in problem[1]


async def test_verify_credential_live_never_calls_out_with_no_credential(
        monkeypatch):
    """An AuthError is the answer, not a reason to spend: the backend is never
    even constructed."""
    from no_human import config as config_mod
    from no_human.agent.backend_check import verify_credential_live

    def _boom(**kw):
        raise config_mod.AuthError("no ANTHROPIC_API_KEY was found")

    monkeypatch.setattr(config_mod, "assert_subscription_mode", _boom)
    seen = _fake_backend(monkeypatch)

    problem = await verify_credential_live(model="claude-haiku-4-5",
                                           auth_mode="api_key")
    assert problem == ("rejected", "no ANTHROPIC_API_KEY was found")
    assert seen["constructed"] == 0


def _doctor(monkeypatch, tmp_path, *args, live=None, config=None):
    """Run the `doctor` COMMAND (not the group) against a tmp DB.

    The command object directly, so the group callback's update notice never
    runs; `load_config` and `check_backend` are replaced so nothing here reads
    the operator's real ~/.no_human. `config`, if given, becomes `_Cfg.data`
    (default `{}`, matching every pre-existing call site byte-for-byte) —
    additive, so callers that don't pass it see identical behaviour to before.
    """
    from click.testing import CliRunner

    import no_human.agent.backend_check as bc_mod
    from no_human.cli import commands as cmd_mod

    class _Cfg:
        data: dict = config if config is not None else {}
        db_path = tmp_path / "doctor.db"
        utility_model = "claude-haiku-4-5"

        def get(self, key, default=None):
            return self.data.get(key, default)

    calls = []

    async def _live(**kw):
        calls.append(kw)
        return live

    monkeypatch.setattr(cmd_mod, "load_config", lambda *a, **k: _Cfg())
    monkeypatch.setattr(bc_mod, "check_backend", lambda **kw: bc_mod.BackendStatus(
        cli_path="/fake/claude", token_present=True))
    monkeypatch.setattr(bc_mod, "verify_credential_live", _live)
    result = CliRunner().invoke(cmd_mod.doctor, list(args))
    return result, calls


def test_doctor_makes_no_live_call_unless_asked(monkeypatch, tmp_path):
    """The default is byte-for-byte what it was: presence only, no spend."""
    result, calls = _doctor(monkeypatch, tmp_path,
                            live=("rejected", "would have failed"))

    assert result.exit_code == 0, result.output
    assert calls == [], "doctor spent quota without being asked"
    assert "presence only — no live auth call" in result.output, result.output


def test_verify_auth_says_so_on_the_auth_line_when_the_credential_works(
        monkeypatch, tmp_path):
    result, calls = _doctor(monkeypatch, tmp_path, "--verify-auth", live=None)

    assert result.exit_code == 0, result.output
    assert len(calls) == 1, "exactly one live call, or the flag is not cheap"
    assert calls[0]["model"] == "claude-haiku-4-5", calls
    assert "verified by one live call" in result.output, result.output
    assert "presence only" not in result.output, result.output


def test_a_transport_failure_does_not_fail_the_doctor_gate(
        monkeypatch, tmp_path):
    """The review's exact demonstration, inverted: a network failure must be
    reported as NOT VERIFIED — never as a dead credential, and never exit 1.
    An operator's cron on a flaky link must not rotate a working token."""
    result, calls = _doctor(
        monkeypatch, tmp_path, "--verify-auth",
        live=("inconclusive",
              "OSError: Network is unreachable — the request never reached "
              "the API, so this says nothing about the credential; try again"))

    assert len(calls) == 1
    assert result.exit_code == 0, (
        f"a transport failure must not fail the gate:\n{result.output}")
    flat = " ".join(result.output.split())  # Rich wraps lines mid-phrase
    assert "NOT VERIFIED (transport failure)" in flat, result.output
    assert "credential not verified" in flat, result.output
    assert "CREDENTIAL DOES NOT WORK" not in flat, result.output


def test_a_credential_the_live_call_rejects_fails_the_doctor_gate(
        monkeypatch, tmp_path):
    """The B5 gap closed: `nh doctor --verify-auth || exit 1` fires on an
    install whose token is present, well-shaped, and dead."""
    result, calls = _doctor(
        monkeypatch, tmp_path, "--verify-auth",
        live=("rejected", "API Error: 401 OAuth token is invalid"))

    assert len(calls) == 1
    assert "CREDENTIAL DOES NOT WORK" in result.output, result.output
    assert "401" in result.output, result.output
    assert "live call REJECTED" in result.output, result.output
    assert result.exit_code == 1, (
        f"a dead credential must fail the gate:\n{result.output}")


# --------------------------------------------------------------------------- #
# codex_row — presence-only summary, no network/subprocess call in api_key    #
# mode, and never surfacing the session probe's raw `detail` in either mode.  #
# --------------------------------------------------------------------------- #

def test_codex_row_api_key_mode_present_when_the_env_file_has_a_key(
        tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=not-a-real-key\n")
    monkeypatch.setattr(cfgmod, "ENV_PATH", env_file)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    row = codex_row({})
    assert row == {
        "mode": "api_key",
        "present": True,
        "model": "gpt-5.3-codex",
        "cli_path": "codex (PATH)",
    }


def test_codex_row_api_key_mode_absent_without_a_key_anywhere(
        tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    row = codex_row({"llm": {"codex_auth_mode": "api_key"}})
    assert row["present"] is False
    assert row["mode"] == "api_key"


def test_codex_row_never_calls_out_in_api_key_mode(tmp_path, monkeypatch):
    """`present` for api_key mode is a pure env/file read — no subprocess, no
    network. A call to `codex_login_status` here would mean this row quietly
    started spending on every `nh doctor` invocation.

    `codex_row` wraps its probe in a bare `except Exception`, so a stub that
    only *raises* proves nothing: the raise would be swallowed into
    `present=False` whether or not the stub ever ran, and the test would
    pass either way. Track calls explicitly instead — that observation
    survives the swallow.
    """
    monkeypatch.setattr(cfgmod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    calls = []

    def _tracking(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("codex_login_status must not run in api_key mode")

    monkeypatch.setattr(_cx, "codex_login_status", _tracking)
    codex_row({"llm": {"codex_auth_mode": "api_key"}})
    assert calls == []


def test_codex_row_subscription_mode_present_on_a_live_chatgpt_session(
        monkeypatch):
    import no_human.agent.codex_backend as cx
    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(
                            True, "chatgpt", detail="account: acct_super_secret"))

    row = codex_row({"llm": {"codex_auth_mode": "subscription"}})
    assert row["present"] is True
    assert row["mode"] == "subscription"
    assert row["model"] == "gpt-5.6-terra"
    # The probe's raw detail (which can echo account identifiers) must never
    # reach the row this command prints.
    assert "acct_super_secret" not in str(row)


def test_codex_row_subscription_mode_absent_when_no_session_is_found(
        monkeypatch):
    import no_human.agent.codex_backend as cx
    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(False, "none"))

    row = codex_row({"llm": {"codex_auth_mode": "subscription"}})
    assert row["present"] is False


def test_codex_row_subscription_mode_absent_for_an_api_key_backed_session(
        monkeypatch):
    """A session `codex login status` reports as api_key-backed does not
    count as a live ChatGPT session for this row, mirroring the same
    refusal `assert_codex_subscription_mode` applies before a run."""
    import no_human.agent.codex_backend as cx
    monkeypatch.setattr(cx, "codex_login_status",
                        lambda cli_path=None: cx.CodexSessionStatus(True, "api_key"))

    row = codex_row({"llm": {"codex_auth_mode": "subscription"}})
    assert row["present"] is False


def test_codex_row_never_raises_when_the_session_probe_blows_up(monkeypatch):
    """A diagnostic must never crash the command that prints it."""
    import no_human.agent.codex_backend as cx

    def _boom(cli_path=None):
        raise OSError("codex binary vanished mid-probe")

    monkeypatch.setattr(cx, "codex_login_status", _boom)
    row = codex_row({"llm": {"codex_auth_mode": "subscription"}})
    assert row["present"] is False


def test_codex_row_honours_an_explicit_model_override_in_either_mode(
        tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    row = codex_row({"llm": {"codex_model": "gpt-5.5"}})
    assert row["model"] == "gpt-5.5"


def test_codex_row_reports_a_cli_path_override(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    row = codex_row({"llm": {"codex_cli_path": "/opt/codex/bin/codex"}})
    assert row["cli_path"] == "/opt/codex/bin/codex"


# --------------------------------------------------------------------------- #
# codex_row through the actual `nh doctor` COMMAND — not just the pure        #
# function above. The CLI wiring (cli/commands.py) is what an operator        #
# actually sees; a unit test on codex_row() alone cannot prove the string     #
# never made it into the rendered line, or that a probe's raw detail didn't   #
# leak through the f-string that assembles it.                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["api_key", "subscription"])
def test_doctor_shows_the_codex_row_in_both_modes(monkeypatch, tmp_path, mode):
    """Reuses the existing `_doctor` CliRunner harness. The probe/env are
    monkeypatched so this never shells out or touches a real credential; the
    injected `detail` string is deliberately something that would be alarming
    to see printed (an account identifier shape) so its absence from the
    output is a meaningful assertion, not a vacuous one."""
    import no_human.agent.codex_backend as cx

    monkeypatch.setattr(cfgmod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    SECRET_DETAIL = "acct_should-never-be-printed_9f3e"
    if mode == "subscription":
        monkeypatch.setattr(
            cx, "codex_login_status",
            lambda *a, **kw: cx.CodexSessionStatus(
                present=True, via="chatgpt", detail=SECRET_DETAIL))
    else:
        (tmp_path / ".env").write_text("OPENAI_API_KEY=not-a-real-key\n")

    result, calls = _doctor(
        monkeypatch, tmp_path, config={"llm": {"codex_auth_mode": mode}})
    assert result.exit_code == 0, result.output
    assert calls == [], "doctor spent quota without being asked"

    lines = [ln for ln in result.output.splitlines() if "codex backend" in ln]
    assert lines, f"no codex backend row at all:\n{result.output}"
    row_line = lines[0]

    assert f"mode: {mode}" in row_line, row_line
    assert "present" in row_line, row_line
    from no_human.agent.backend import default_codex_model
    assert default_codex_model(mode) in row_line, row_line

    # Never the raw probe detail, never a credential value, anywhere in the
    # whole rendered output — not just the one row.
    assert SECRET_DETAIL not in result.output, result.output
    assert "not-a-real-key" not in result.output, result.output


def test_the_codex_row_makes_no_live_call(monkeypatch, tmp_path):
    """The default `nh doctor` invocation (no --verify-auth) must compute the
    codex row without any subprocess or live-credential call: `verify_credential_live`
    (Claude probe) must be entirely unreached (`calls == []`, patched to
    record rather than run), and `codex_login_status` — the only thing in
    api_key mode's own code path that COULD shell out — must never be called
    either, proven by making it explode rather than merely asserting a mock
    wasn't invoked afterward. (Blocking `socket.socket` outright was tried
    first and rejected: asyncio's own event loop opens a local self-pipe via
    `socket.socketpair()` as plumbing, unrelated to any credential call, so
    that blocks command startup itself rather than testing anything real.)
    """
    import no_human.agent.codex_backend as cx

    def _no_probe(*_a, **_kw):
        raise AssertionError("codex_login_status must not be called in the "
                              "default (api_key) doctor invocation")

    monkeypatch.setattr(cfgmod, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cx, "codex_login_status", _no_probe)

    result, calls = _doctor(monkeypatch, tmp_path)
    assert result.exit_code == 0, result.output
    assert calls == [], "the Claude live-verify probe ran without --verify-auth"
    assert "codex backend" in result.output, result.output


def test_a_typo_d_codex_auth_mode_is_a_contradiction_not_a_traceback(
        monkeypatch, tmp_path):
    """`codex_row` reads `llm.codex_auth_mode` outside any guard in the CLI
    wiring; a typo'd value used to raise `AuthError` straight through
    `nh doctor`, even on an install that doesn't use codex at all
    (`worker.backend: claude`) — contradicting `codex_row`'s own docstring
    ("a diagnostic must never crash the command that prints it"). It must
    come back as a readable contradiction with a nonzero exit, never a
    traceback."""
    result, calls = _doctor(
        monkeypatch, tmp_path,
        config={"worker": {"backend": "claude"},
                "llm": {"codex_auth_mode": "bogus-mode"}})

    # CliRunner always reports a `SystemExit` here (that's how Click's own
    # exit-code machinery works, even on a clean nonzero exit) — the crash
    # this test guards against is an *unhandled* `AuthError` reaching the
    # command, which would show up as a different exception type or as a
    # printed traceback, not as a plain `SystemExit`.
    assert result.exc_info[0] is SystemExit, (
        f"nh doctor raised instead of reporting a contradiction:\n"
        f"{result.output}\n{result.exc_info}")
    assert "Traceback" not in result.output, result.output
    assert result.exit_code != 0, result.output
    assert "bogus-mode" in result.output, result.output
    flat = " ".join(result.output.split())
    assert "CODEX CONFIG INVALID" in flat, result.output
