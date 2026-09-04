"""A human's ``resume_from`` must be EXECUTED once, never DECIDED over forever.

INCIDENT: once a human ran ``nh task resume``, ``resume_from.by == "human"``
never expired — every later AUTOMATIC checkpoint (server stop, orphan
requeue, hard-kill salvage) refused to stamp over it, forever, because the
"never overwrite a human's gate" rule made no distinction between an ARMED
gate (not yet executed — must not be overwritten) and one already EXECUTED
by the very next attempt (now just an ordinary checkpoint that later
machine stamps must be free to move forward). Three production tasks
(916eeeca, ae4723a2, 110655e5) each lost a large amount of work this way:
every successor attempt restarted from the original human sha instead of
the newest WIP, discarding everything committed in between.

The fix is consume-once semantics: the attempt that actually branches from a
human's gated sha rewrites ``by: "human"`` -> ``by: "consumed_human"``
(`Orchestrator._consume_human_gate`) once its workspace exists. sha/branch
are left untouched — the audit trail still shows a human chose this branch
point, and the zero-diff honesty gate still credits it as theirs
(`is_human_provenance`) — but the gate itself is no longer ARMED
(`human_gate_armed`), so ordinary machine stamping resumes. A fresh
``nh task resume`` writes ``"human"`` again and re-arms it.

Fixtures (`store`, `bare_repo`, `_git`, `_incident_result`, `_run_one_attempt`)
come from tests/test_infra_not_work.py, the same shape
tests/test_server_stop_checkpoint.py is already tested against.
"""

from __future__ import annotations

import subprocess

from no_human.blockers import (
    CONSUMED_HUMAN_PROVENANCE,
    SERVER_STOP_REASON,
    human_gate_armed,
    is_human_provenance,
    resume_provenance,
)
from no_human.core.orchestrator import Orchestrator
from no_human.core.scheduler import Scheduler
from no_human.core.task import Task, TaskStatus
from tests.test_infra_not_work import (  # noqa: F401 — fixtures re-exported on purpose
    _git,
    _incident_result,
    _run_one_attempt,
    bare_repo,
)
from tests.test_scheduler import _age_row


def _head(cwd) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def _gate_armed_for_credit(ctx: dict, sha: str) -> bool:
    """Ask `_is_own_partial` the credit question directly, the way
    tests/test_scheduler.py's `_gate_armed` asks it for the shape-based
    paths — but here `resume_from.sha == sha`, so provenance alone decides
    and no repo/subject is ever read."""
    return Orchestrator._is_own_partial(
        Orchestrator.__new__(Orchestrator), None, ctx, sha)


# --------------------------------------------------------------------------- #
# AC — the human sha is honored by the immediately-next attempt (pin)          #
# --------------------------------------------------------------------------- #

async def test_the_next_attempt_after_a_human_resume_starts_from_the_human_sha(
        store, bare_repo, tmp_path):  # noqa: F811 — fixture params shadow the re-export by design
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    _git(bare_repo, "checkout", "-q", "-b", "no-human/human-gated")
    (bare_repo / "human.py").write_text("human work\n")
    _git(bare_repo, "add", "human.py")
    _git(bare_repo, "commit", "-qm", "[WIP-BLOCKED] answered by a human")
    human_sha = _head(bare_repo)
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(
            {"sha": human_sha, "branch": "no-human/human-gated"}, "human")})
    ctx = task.context

    assert orch._resume_branch_point(repo, ctx, 1) == human_sha
    # Disarmed FOR CREDIT: a fresh attempt that branches here and adds
    # nothing must not be failed as fabrication.
    assert _gate_armed_for_credit(ctx, human_sha) is False


def test_a_consumed_gate_is_still_credited_as_a_humans_branch_point():
    """Guards the D15 direction: the moment a gate is consumed it must not
    suddenly stop being credited as the human's own work."""
    ctx = {"resume_from": {"sha": "a" * 40, "by": CONSUMED_HUMAN_PROVENANCE}}
    assert _gate_armed_for_credit(ctx, "a" * 40) is False
    assert is_human_provenance(CONSUMED_HUMAN_PROVENANCE, ctx) is True


# --------------------------------------------------------------------------- #
# AC — RED-FIRST CORE: a server-stop checkpoint updates a CONSUMED gate        #
# --------------------------------------------------------------------------- #

async def _implementing(store, task):  # noqa: F811 — fixture param shadows the re-export by design
    await store.set_status(task, TaskStatus.IMPLEMENTING)
    return await store.get_task(task.id)


async def test_a_server_stop_checkpoint_updates_a_consumed_human_resume_from(
        store, bare_repo, tmp_path):  # noqa: F811 — fixture params shadow the re-export by design
    """THE regression test. Fails on unfixed code: `_honor_server_stop` reads
    the inlined ``by == "human"`` check, which also matches
    ``"consumed_human"`` == False today only by accident of string equality
    — no, on UNFIXED code there is no `consumed_human` concept at all, so a
    human resume that was already executed by a prior attempt still carries
    ``by == "human"`` forever and every later server-stop checkpoint is
    refused: `resume_from` never updates past the original human sha, and
    the successor attempt discards everything committed since."""
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task = await _implementing(store, task)
    _git(bare_repo, "checkout", "-q", "-b", "no-human/live")
    (bare_repo / "human.py").write_text("human work\n")
    _git(bare_repo, "add", "human.py")
    _git(bare_repo, "commit", "-qm", "[WIP-BLOCKED] answered by a human")
    human_sha = _head(bare_repo)
    # Simulate: an attempt already branched from the human's gate and
    # consumed it (`_consume_human_gate`) — sha/branch preserved, `by`
    # rewritten.
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(
            {"sha": human_sha, "branch": "no-human/live"},
            CONSUMED_HUMAN_PROVENANCE)})
    # That attempt then does new work and the server stops mid-session.
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    aid = await store.create_attempt(task.id, 2)
    orch.request_server_stop()

    outcome = await orch._honor_cancel(
        task, repo, "no-human/live", SERVER_STOP_REASON, attempt_id=aid)

    fresh = await store.get_task(task.id)
    rf = fresh.context["resume_from"]
    assert outcome.status == TaskStatus.IMPLEMENTING
    assert rf["by"] == "server_stop", (
        f"a consumed human gate must not block a server-stop checkpoint; got {rf!r}")
    assert rf["sha"] != human_sha, "the newer WIP commit must be stamped, not the stale human sha"
    assert _head(bare_repo) == rf["sha"]
    row = next(r for r in await store.list_attempts(task.id) if r["id"] == aid)
    assert "checkpointed" in row["failure_reason"]
    assert "behind a human-gated resume_from" not in row["failure_reason"]


# --------------------------------------------------------------------------- #
# AC — consumption actually happens once an attempt starts                    #
# --------------------------------------------------------------------------- #

async def test_starting_an_attempt_from_a_human_gate_consumes_it(store, tmp_path):  # noqa: F811 — fixture param shadows the re-export by design
    task = Task.new("do a thing", repo_path="/tmp/x")
    await store.create_task(task)
    human_sha = "a" * 40
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(
            {"sha": human_sha, "branch": "no-human/gate"}, "human")})
    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store

    ctx = await orch._consume_human_gate(task, task.context)

    assert ctx["resume_from"] == {
        "sha": human_sha, "branch": "no-human/gate", "by": CONSUMED_HUMAN_PROVENANCE}
    stored = (await store.get_task(task.id)).context
    assert stored["resume_from"] == ctx["resume_from"]
    assert human_gate_armed(ctx) is False

    # Idempotent: consuming an already-consumed gate is a no-op.
    ctx2 = await orch._consume_human_gate(task, ctx)
    assert ctx2["resume_from"] == ctx["resume_from"]


async def test_a_legacy_unstamped_resume_from_is_armed_and_consumable(store):  # noqa: F811 — fixture param shadows the re-export by design
    task = Task.new("do a thing", repo_path="/tmp/x")
    await store.create_task(task)
    legacy_sha = "b" * 40
    task.context = await store.merge_context(
        task.id, {"resume_from": {"sha": legacy_sha, "branch": "no-human/legacy"}})
    orch = Orchestrator.__new__(Orchestrator)
    orch.store = store

    assert human_gate_armed(task.context) is True

    ctx = await orch._consume_human_gate(task, task.context)

    assert ctx["resume_from"]["by"] == CONSUMED_HUMAN_PROVENANCE
    assert ctx["resume_from"]["sha"] == legacy_sha


# --------------------------------------------------------------------------- #
# AC — an ARMED gate still blocks every automatic checkpoint (regression)      #
# --------------------------------------------------------------------------- #

async def test_an_armed_human_gate_still_blocks_the_server_stop_stamp(
        store, bare_repo, tmp_path):  # noqa: F811 — fixture params shadow the re-export by design
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task = await _implementing(store, task)
    _git(bare_repo, "checkout", "-q", "-b", "no-human/live")
    (bare_repo / "human.py").write_text("human work\n")
    _git(bare_repo, "add", "human.py")
    _git(bare_repo, "commit", "-qm", "[WIP-BLOCKED] answered by a human")
    human_sha = _head(bare_repo)
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(
            {"sha": human_sha, "branch": "no-human/live"}, "human")})
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    aid = await store.create_attempt(task.id, 2)
    orch.request_server_stop()

    await orch._honor_cancel(
        task, repo, "no-human/live", SERVER_STOP_REASON, attempt_id=aid)

    fresh = await store.get_task(task.id)
    assert fresh.context["resume_from"] == {
        "sha": human_sha, "branch": "no-human/live", "by": "human"}
    row = next(r for r in await store.list_attempts(task.id) if r["id"] == aid)
    assert "behind a human-gated resume_from, not resumed onto" in row["failure_reason"]


async def test_an_armed_human_gate_still_blocks_orphan_requeue(store):  # noqa: F811 — fixture param shadows the re-export by design
    """Control for `Scheduler._inherited_checkpoint`: an ARMED gate is
    inherited untouched, exactly as before this fix."""
    t = Task.new("resumed by a human, then killed", repo_path="/r")
    await store.create_task(t)
    human_sha = "c" * 40
    await store.merge_context(
        t.id, {"resume_from": resume_provenance({"sha": human_sha}, "human")})
    await store.set_status(t, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(t.id, 1)
    await store.update_attempt(attempt_id, commit_sha="d" * 40)
    await _age_row(store, t.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    resume_from = ((await store.get_task(t.id)).context or {})["resume_from"]
    assert resume_from == {"sha": human_sha, "by": "human"}


# --------------------------------------------------------------------------- #
# AC — a requeue after a CONSUMED gate inherits the newest attempt's sha       #
# --------------------------------------------------------------------------- #

async def test_a_requeue_after_a_consumed_gate_inherits_the_newest_attempt_sha(store):  # noqa: F811 — fixture param shadows the re-export by design
    """Today (unfixed) this returns the stale consumed sha unchanged — a
    `consumed_human` stamp reads exactly like an armed `human` one and the
    successor requeue restarts from the original human sha, discarding
    whatever the dead attempt committed since."""
    t = Task.new("resumed by a human, executed, then killed", repo_path="/r")
    await store.create_task(t)
    human_sha = "e" * 40
    await store.merge_context(
        t.id, {"resume_from": resume_provenance(
            {"sha": human_sha, "branch": "no-human/gate"}, CONSUMED_HUMAN_PROVENANCE)})
    await store.set_status(t, TaskStatus.TESTING, validate=False)
    attempt_id = await store.create_attempt(t.id, 1)
    newer_sha = "f" * 40
    await store.update_attempt(attempt_id, commit_sha=newer_sha)
    await _age_row(store, t.id)

    await Scheduler(store, lambda task=None: None)._recover_orphans()

    resume_from = ((await store.get_task(t.id)).context or {})["resume_from"]
    assert resume_from["sha"] == newer_sha, (
        f"a consumed gate must not block orphan requeue; got {resume_from!r}")
    assert resume_from["by"] == "orphan_recovery"


# --------------------------------------------------------------------------- #
# AC — a second human resume re-arms the gate                                  #
# --------------------------------------------------------------------------- #

async def test_a_second_human_resume_re_arms_the_gate(store, bare_repo, tmp_path):  # noqa: F811 — fixture params shadow the re-export by design
    orch, _backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _incident_result())
    task = await _implementing(store, task)
    _git(bare_repo, "checkout", "-q", "-b", "no-human/live")
    (bare_repo / "human.py").write_text("human work\n")
    _git(bare_repo, "add", "human.py")
    _git(bare_repo, "commit", "-qm", "[WIP-BLOCKED] first human answer")
    first_sha = _head(bare_repo)
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(
            {"sha": first_sha, "branch": "no-human/live"}, CONSUMED_HUMAN_PROVENANCE)})
    assert human_gate_armed(task.context) is False

    # `nh task resume` / its API twin write "human" verbatim — that write
    # re-arms the gate. Modeled on the CLI's own write shape (`resume_provenance`
    # with `by="human"`), not on any renamed helper.
    (bare_repo / "human2.py").write_text("second human answer\n")
    _git(bare_repo, "add", "human2.py")
    _git(bare_repo, "commit", "-qm", "[WIP-BLOCKED] second human answer")
    second_sha = _head(bare_repo)
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(
            {"sha": second_sha, "branch": "no-human/live"}, "human")})
    assert human_gate_armed(task.context) is True

    (bare_repo / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    aid = await store.create_attempt(task.id, 2)
    orch.request_server_stop()

    await orch._honor_cancel(
        task, repo, "no-human/live", SERVER_STOP_REASON, attempt_id=aid)

    fresh = await store.get_task(task.id)
    assert fresh.context["resume_from"] == {
        "sha": second_sha, "branch": "no-human/live", "by": "human"}, (
        "a re-armed gate must be left untouched by the next automatic checkpoint")
