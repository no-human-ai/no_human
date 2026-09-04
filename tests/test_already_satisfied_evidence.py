"""The already-satisfied terminal must leave typed PR evidence and a READY
PR, and one shared predicate must answer "does this task have a PR" for
task_pr, doctor and restore-approval.

Root cause: `orchestrator._gate_already_satisfied`'s PASS branch used to
enter AWAITING_APPROVAL with only a generic `"state"` event — no typed
evidence, no promotion of an existing draft PR to ready — so a human could
end up approving a DRAFT PR. Meanwhile three different places answered "does
this task have a PR/legitimate completion evidence" with three different,
inconsistent sets: `vcs/task_pr.py`'s `_PR_EVENT_KINDS` (pr_open, pr_draft),
`doctor.py`'s `REQUIRED_EVIDENCE` (pr_open only), and `cli/commands.py`'s
`restore-approval` (pr_open only, via a literal string check that
contradicted its own preceding `task_has_pr_evidence` call).

Fix shape:
  (a) `_gate_already_satisfied`'s PASS branch always emits a typed
      `already_satisfied` event, and best-effort promotes a resolved draft
      PR to ready via `gh pr ready`, emitting `pr_open` for the promoted URL.
  (b) One predicate/constant for PR evidence kinds
      (`PR_EVENT_KINDS`/`AWAITING_APPROVAL_EVIDENCE_KINDS`), and one for
      legitimate DONE evidence (`DONE_EVIDENCE_KINDS`), all defined once in
      `vcs/task_pr.py` and imported (never redefined) by `doctor.py` and
      `cli/commands.py`.
  (c) doctor's evidence-gap message for a still-unbacked task names the
      accepted kinds.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import unittest.mock as mock

import pytest
from click.testing import CliRunner

from no_human.agent.claude_backend import AgentResult
from no_human.cli.commands import task_restore_approval
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.doctor import diagnose
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewDecision
from no_human.review.selfcheck import ChecklistItem
from no_human.vcs import comment_poster, github
from no_human.vcs.task_pr import (
    AWAITING_APPROVAL_EVIDENCE_KINDS,
    DONE_EVIDENCE_KINDS,
    PR_EVENT_KINDS,
    task_has_pr_evidence,
)

import no_human.cli.commands as cmd_mod
import no_human.doctor as doctor_mod


# --------------------------------------------------------------------------- #
# git/orchestrator fixtures — copied from tests/test_e2e_orchestrator.py      #
# --------------------------------------------------------------------------- #

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


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
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _spy_gh_ready(monkeypatch, *, returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Patch `vcs.github`'s `subprocess.run` so a `gh pr ready` call is
    answered with a canned CompletedProcess, while every OTHER subprocess
    call (git plumbing the orchestrator makes along the way) runs for real.

    This exercises the REAL production path end to end:
    `promote_draft_pr`'s remote-routing (`is_github_remote` against the
    repo's actual remote url) AND `github.mark_pr_ready`'s subprocess call
    and outcome-token parsing — not a stand-in for either. A prior version
    of this test stubbed `orchestrator.promote_draft_pr` wholesale, which
    let both of those pieces of new production logic go untested.
    """
    real_run = subprocess.run
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if argv[:1] == ["gh"]:
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    return calls


def _spy_gh_full(monkeypatch, *, pr_url: str | None = None, ready_returncode: int = 0):
    """Like `_spy_gh_ready`, but also answers `gh pr create` ("already
    exists"), `gh pr list` (resolves the existing PR) and `gh pr edit`
    (captures the rebuilt body) — the extra calls the body-refresh path
    (`vcs/__init__.py:open_pr` -> `github.open_pr`) makes, on top of the
    checklist-comment `gh api` calls and `gh pr ready` promotion
    `_spy_gh_ready` already answers. Every OTHER subprocess call (git
    plumbing) runs for real."""
    real_run = subprocess.run
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if argv[:1] != ["gh"]:
            return real_run(argv, **kwargs)
        calls.append(list(argv))
        if argv[1:3] == ["pr", "create"]:
            return subprocess.CompletedProcess(
                argv, 1, "",
                'a pull request for branch "x" into branch "main" already exists')
        if argv[1:3] == ["pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, (pr_url or "") + "\n", "")
        if argv[1:3] == ["pr", "edit"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:3] == ["pr", "ready"]:
            return subprocess.CompletedProcess(argv, ready_returncode, "", "")
        # gh api ... — the checklist comment's read (GET, no -X) and post
        # (POST, -X). No marker in the (empty) existing-comments read.
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    return calls


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


class AlreadySatisfiedBackend:
    """Zero edits + a fully-cited ALREADY-SATISFIED per-criterion claim.
    Copied from tests/test_e2e_orchestrator.py's AlreadySatisfiedBackend."""

    STATEMENT = (
        "Verified every criterion against the existing code.\n"
        "ALREADY-SATISFIED\n"
        "CRITERION: mul(a,b) returns product — MET — evidence: calc.py:4\n"
    )

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        return AgentResult(final_text=self.STATEMENT, num_turns=3, is_error=False,
                           tokens_used=120, session_id="s", stop_reason="end_turn")


class FakeReviewer:
    """Injects a scripted ReviewDecision without running the LLM. Copied
    from tests/test_e2e_orchestrator.py's FakeReviewer."""

    def __init__(self, decision: ReviewDecision):
        self._decision = decision
        self.calls: list[dict] = []

    async def review(self, task, *, repo_path, test_output="", held_out_output="",
                     before_ref="HEAD~1", after_ref="HEAD", **kwargs):
        self.calls.append({"task_id": task.id, "mode": kwargs.get("mode"),
                           "claim_report": kwargs.get("claim_report")})
        return self._decision


_PASSING = ReviewDecision(passed=True, checklist=[
    ChecklistItem("mul(a,b) returns product", True,
                  "calc.py:4 defines mul returning a*b")])


# --------------------------------------------------------------------------- #
# AC1 — the already-satisfied terminal always emits typed evidence, and      #
# best-effort promotes a draft PR to ready.                                  #
# --------------------------------------------------------------------------- #

async def test_already_satisfied_terminal_leaves_typed_evidence_and_a_ready_pr(
    bare_repo, tmp_path, store, monkeypatch
):
    """A task that walks in carrying a DRAFT PR (opened pre-review, per the
    0a flow) and is then found already-satisfied by the review gate must
    leave: (1) a typed `already_satisfied` event — not just a generic
    `"state"` event a human can't distinguish from any other park — and
    (2) the draft PR promoted to ready via `gh pr ready`, evidenced by a
    `pr_ready` event and then a `pr_open` event for the same URL. Approving
    a task off a DRAFT PR is exactly the failure this closes."""
    reviewer = FakeReviewer(_PASSING)
    cfg = _config(tmp_path)
    # The bare fixture's remote is a local path, not github.com — classify
    # it via the documented GHE `git.github_hosts` mechanism (same pattern
    # as tests/test_e2e_orchestrator.py's 0a tests) so `promote_draft_pr`'s
    # real `is_github_remote` routing takes the GitHub branch.
    cfg.data.setdefault("git", {})["github_hosts"] = ["remote.git"]
    events = []
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer,
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {
        "eval_result": {"verdict": "accept"},
        "pr_draft_created": "https://github.com/acme/widgets/pull/9",
        "pr_draft_branch": "no-human/add-mul",
    }
    await store.create_task(t)

    gh_calls = _spy_gh_ready(monkeypatch)

    outcome = await orch.run_task(t)
    await store.save_events(t.id, events)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url == "https://github.com/acme/widgets/pull/9", outcome.pr_url
    # The already-satisfied gate now also posts the review checklist comment
    # (via the SAME `_post_review_checklist_comment` helper `_finalize` uses)
    # before promoting the draft: a read of existing comments (no marker
    # found), then the post itself, then the real `gh pr ready <url>` argv
    # produced by github.mark_pr_ready via promote_draft_pr's remote-routing.
    assert len(gh_calls) == 3, gh_calls
    assert gh_calls[0] == ["gh", "api", "--hostname", "github.com", "--paginate",
                           "repos/acme/widgets/issues/9/comments?per_page=100"], gh_calls
    assert gh_calls[1][:6] == ["gh", "api", "--hostname", "github.com", "-X", "POST"], \
        gh_calls
    assert gh_calls[1][6] == "repos/acme/widgets/issues/9/comments", gh_calls
    assert gh_calls[2] == ["gh", "pr", "ready",
                           "https://github.com/acme/widgets/pull/9"], gh_calls

    review_comments = [e for e in events if e.get("kind") == "review_comment"]
    assert review_comments and review_comments[-1].get("status") == "posted", events

    already = [e for e in events if e.get("kind") == "already_satisfied"]
    assert already, events
    assert already[-1]["review_round"] >= 1, already[-1]
    assert already[-1]["pr_url"] == "https://github.com/acme/widgets/pull/9"

    ready_events = [e for e in events if e.get("kind") == "pr_ready"]
    assert ready_events, events
    assert ready_events[-1]["pr_url"] == "https://github.com/acme/widgets/pull/9"

    opens = [e for e in events if e.get("kind") == "pr_open"]
    assert opens, events
    assert opens[-1].get("text") == "https://github.com/acme/widgets/pull/9" or \
        opens[-1].get("pr_url") == "https://github.com/acme/widgets/pull/9"

    # Persisted, not just in-memory — doctor and the CLI read from SQLite.
    persisted = await store.list_events(t.id)
    assert any(e.get("kind") == "already_satisfied" for e in persisted)
    assert any(e.get("kind") == "pr_open" for e in persisted)

    # doctor's shared predicate now sees this AWAITING_APPROVAL task as backed.
    d = await diagnose(store)
    assert not any(t.id[:8] in g for g in d.evidence_gaps), d.evidence_gaps

    # task_pr's shared predicate agrees it's the same PR.
    assert await task_has_pr_evidence(store, t) == \
        "https://github.com/acme/widgets/pull/9"


async def test_already_satisfied_terminal_emits_typed_evidence_with_no_pr(
    bare_repo, tmp_path, store
):
    """A task with no draft PR at all (nothing to promote) must still leave
    typed `already_satisfied` evidence — the fix does not make PR promotion
    a precondition for emitting the typed event."""
    reviewer = FakeReviewer(_PASSING)
    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer,
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    outcome = await orch.run_task(t)
    await store.save_events(t.id, events)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None

    already = [e for e in events if e.get("kind") == "already_satisfied"]
    assert already, events
    assert not already[-1].get("pr_url")
    assert not any(e.get("kind") == "pr_ready" for e in events), events
    assert not any(e.get("kind") == "pr_open" for e in events), events


async def test_already_satisfied_terminal_records_advisory_when_promotion_fails(
    bare_repo, tmp_path, store, monkeypatch
):
    """A draft PR that `gh pr ready` refuses to promote (e.g. closed) must
    NOT be advertised as `pr_open` — it stays unbacked for approval evidence
    — but the typed `already_satisfied` event is still emitted (evidence is
    unconditional), and the refusal is recorded as an advisory so `nh doctor`
    can enumerate it."""
    reviewer = FakeReviewer(_PASSING)
    cfg = _config(tmp_path)
    # See the sibling "ready" test above: classify the local bare-repo
    # remote as GitHub via github_hosts so promote_draft_pr's real routing
    # (not a stub) dispatches to github.mark_pr_ready.
    cfg.data.setdefault("git", {})["github_hosts"] = ["remote.git"]
    events = []
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer,
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {
        "eval_result": {"verdict": "accept"},
        "pr_draft_created": "https://github.com/acme/widgets/pull/9",
        "pr_draft_branch": "no-human/add-mul",
    }
    await store.create_task(t)

    # A real (spied) `gh pr ready` call that refuses because the PR is
    # closed — the stderr deliberately does NOT contain both "already" and
    # "ready", so github.mark_pr_ready's real parsing takes the `refused:`
    # branch rather than misclassifying this as `already_ready`.
    gh_calls = _spy_gh_ready(
        monkeypatch, returncode=1, stderr="GraphQL: pull request is closed"
    )

    outcome = await orch.run_task(t)
    await store.save_events(t.id, events)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None, outcome.pr_url  # never advertised as ready

    already = [e for e in events if e.get("kind") == "already_satisfied"]
    assert already, events
    assert already[-1]["pr_url"] == "https://github.com/acme/widgets/pull/9"

    ready_events = [e for e in events if e.get("kind") == "pr_ready"]
    assert ready_events, events
    # The real gh pr ready outcome-token parsing, not a stub's fixed string:
    # `mark_pr_ready` turns a non-"already ready" refusal into
    # "refused: <first stderr line>", embedded in the emitted event text.
    assert "refused: GraphQL: pull request is closed" in ready_events[-1].get("text", ""), \
        ready_events

    assert not any(e.get("kind") == "pr_open" for e in events), events
    advisories = [e for e in events if e.get("kind") == "advisory"]
    assert any("widgets/pull/9" in (e.get("text") or "") for e in advisories), \
        advisories

    # And the real `gh pr ready <url>` subprocess call actually happened —
    # promote_draft_pr's remote-routing dispatched to github.mark_pr_ready
    # rather than a stub answering for it. The checklist-comment attempt also
    # ran first: this spy returns returncode=1 for EVERY gh call, so reading
    # existing comments fails too ("unverifiable") and no POST is attempted —
    # `post_to_pr_once` never risks a duplicate post on an unreadable PR.
    assert gh_calls == [
        ["gh", "api", "--hostname", "github.com", "--paginate",
         "repos/acme/widgets/issues/9/comments?per_page=100"],
        ["gh", "pr", "ready", "https://github.com/acme/widgets/pull/9"],
    ], gh_calls


# --------------------------------------------------------------------------- #
# AC2 — doctor and restore-approval read the same, now-widened, evidence     #
# kinds instead of a hardcoded pr_open-only set.                             #
# --------------------------------------------------------------------------- #

def _ev(kind: str, **extra) -> dict:
    return {"source": "test", "kind": kind, "text": "", "ts": time.time(), **extra}


async def test_doctor_accepts_landed_override_human_merged_and_already_satisfied_done_evidence(
    tmp_path,
):
    """Every kind in DONE_EVIDENCE_KINDS backs a DONE task without a gap, and
    an AWAITING_APPROVAL task backed only by the new `already_satisfied`
    event (no PR at all — the zero-diff already-satisfied case) is not
    flagged either. A task with none of the accepted kinds is still flagged,
    and its message names every accepted kind — doctor never widens beyond
    the two REQUIRED_EVIDENCE entries, it only widens the SET each already
    named."""
    store = await Store(tmp_path / "nh.db").connect()
    try:
        done_tasks = {}
        for kind in sorted(DONE_EVIDENCE_KINDS):
            t = Task.new(f"done via {kind}", repo_path="/tmp/x")
            await store.create_task(t)
            await store.save_events(t.id, [_ev(kind)])
            # set_status(..., DONE) REQUIRES an event= dict (SilentCompletion
            # otherwise); reuse the same kind as the completion event so the
            # DONE write itself is legitimately evidenced, matching how a
            # real completion records it.
            await store.set_status(t, TaskStatus.DONE, validate=False,
                                   event=_ev(kind))
            done_tasks[kind] = t

        awaiting = Task.new("already-satisfied, no PR", repo_path="/tmp/x")
        await store.create_task(awaiting)
        await store.save_events(awaiting.id, [_ev("already_satisfied", pr_url="")])
        await store.set_status(awaiting, TaskStatus.AWAITING_APPROVAL, validate=False)

        # A genuinely unbacked DONE task: set_status() itself refuses a
        # silent DONE write (SilentCompletion), so reproduce the exact
        # pre-fix false-done shape the way tests/test_false_done_completion.py
        # does — bypass the guard with a direct row UPDATE, no event at all.
        unbacked = Task.new("done with nothing on record", repo_path="/tmp/x")
        await store.create_task(unbacked)
        await store.db.execute(
            "UPDATE tasks SET status = ? WHERE id = ?",
            (TaskStatus.DONE.value, unbacked.id),
        )
        await store.db.commit()

        d = await diagnose(store)

        # every DONE_EVIDENCE_KINDS-backed task is accepted, no gap
        for kind, t in done_tasks.items():
            assert not any(t.id[:8] in g for g in d.evidence_gaps), \
                (kind, d.evidence_gaps)

        # the already_satisfied-only AWAITING_APPROVAL task is accepted too
        assert not any(awaiting.id[:8] in g for g in d.evidence_gaps), d.evidence_gaps

        # a genuinely unbacked DONE task is still flagged, and the message
        # names every accepted kind (AC (c))
        unbacked_gaps = [g for g in d.evidence_gaps if unbacked.id[:8] in g]
        assert unbacked_gaps, d.evidence_gaps
        for kind in DONE_EVIDENCE_KINDS:
            assert kind in unbacked_gaps[0], unbacked_gaps[0]
    finally:
        await store.close()


async def test_doctor_pr_event_kinds_is_the_same_object_task_pr_exports(tmp_path):
    """AC3: one predicate. doctor.py must import (not redefine) task_pr's
    constants — `is` holds only if it's the SAME frozenset object."""
    assert doctor_mod.PR_EVENT_KINDS is PR_EVENT_KINDS
    assert doctor_mod.AWAITING_APPROVAL_EVIDENCE_KINDS is AWAITING_APPROVAL_EVIDENCE_KINDS
    assert doctor_mod.DONE_EVIDENCE_KINDS is DONE_EVIDENCE_KINDS
    assert PR_EVENT_KINDS < AWAITING_APPROVAL_EVIDENCE_KINDS  # proper subset
    assert "already_satisfied" in AWAITING_APPROVAL_EVIDENCE_KINDS
    assert "pr_draft" in PR_EVENT_KINDS


def test_cli_commands_pr_event_kinds_is_the_same_object_task_pr_exports():
    """AC3: `cli/commands.py`'s restore-approval must check the SAME
    PR_EVENT_KINDS object task_pr.task_has_pr_evidence itself resolves
    against — not a hardcoded `== "pr_open"` literal that contradicts it."""
    assert cmd_mod.PR_EVENT_KINDS is PR_EVENT_KINDS


# --------------------------------------------------------------------------- #
# AC2 — restore-approval now accepts pr_draft-only evidence, matching        #
# task_has_pr_evidence (which it already calls first).                       #
# --------------------------------------------------------------------------- #

class _Cfg:
    db_path = None
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)


def _cfg(db_path):
    c = _Cfg()
    c.db_path = db_path
    return c


def _invoke(cmd, db, args):
    with mock.patch.object(cmd_mod, "_bootstrap",
                           lambda require_auth=False: (_cfg(db), None)):
        return CliRunner().invoke(cmd, args)


def _task_state(db, task_id):
    async def _go():
        async with Store(db) as store:
            t = await store.get_task(task_id)
            events = await store.list_events(task_id)
            return t, events
    return asyncio.run(_go())


def test_gate_already_satisfied_docstring_describes_the_pr_refresh():
    """The docstring's PASS clause used to read "the claim is the deliverable,
    there is no PR" — true when this gate was written, false once the gate
    grew a PR-refresh terminal (rebuild the body via the same evidence chain
    `_finalize` uses, post the review-checklist comment, promote the draft) —
    a review caught the stale claim. Pin the fix: the false absolute must be
    gone and the refresh behavior must be named.
    """
    doc = Orchestrator._gate_already_satisfied.__doc__ or ""
    assert "there is no PR" not in doc
    assert "refresh" in doc.lower()
    assert "checklist" in doc.lower()


def test_restore_approval_accepts_pr_draft_only_evidence(tmp_path):
    """The stranded shape this fix closes: an ESCALATED task whose only PR
    evidence is a `pr_draft` event (the PR was opened pre-review per the 0a
    flow, and review never got far enough to promote/record a `pr_open`).
    `task_has_pr_evidence` (called first) already resolves this via
    `ctx["pr_draft_created"]`/PR_EVENT_KINDS; the second gate right after it
    used to hardcode `== "pr_open"` and refuse anyway. Both checks must now
    agree.

    Mutation to state in the PR body: revert the second gate to
    `e.get("kind") == "pr_open"` -> this test fails with exit_code == 1 and
    output containing "no PR event on record".
    """
    db = tmp_path / "nh.db"

    async def _setup():
        async with Store(db) as store:
            t = Task.new("escalated, draft PR only", repo_path="/tmp/x")
            t.context = {"pr_draft_created": "https://example.invalid/pr/9",
                         "pr_draft_branch": "no-human/x"}
            await store.create_task(t)
            await store.save_events(t.id, [{
                "source": "test", "kind": "pr_draft",
                "text": "https://example.invalid/pr/9", "ts": 0.0}])
            await store.set_status(t, TaskStatus.ESCALATED, validate=False,
                                   human_override=True)
            return t.id
    tid = asyncio.run(_setup())

    result = _invoke(task_restore_approval, db, [tid, "--reason", "probe"])

    assert result.exit_code == 0, result.output
    t, events = _task_state(db, tid)
    assert t.status is TaskStatus.AWAITING_APPROVAL
    repaired = [e for e in events if e.get("kind") == "human_restore_approval"]
    assert len(repaired) == 1, events
    assert (t.context or {}).get("pr_closed_repaired_url") == \
        "https://example.invalid/pr/9"


# --------------------------------------------------------------------------- #
# already-satisfied PR delivery must reuse _finalize's own two helpers:      #
# _pr_body (via _gather_evidence) and _post_review_checklist_comment.        #
# --------------------------------------------------------------------------- #

def _head_sha(work):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True,
        text=True, check=True,
    ).stdout.strip()


async def test_already_satisfied_rebuilds_the_pr_body_through_pr_body(
    bare_repo, tmp_path, store, monkeypatch
):
    """AC1: a PR delivered through the already-satisfied gate must not keep
    its pre-gate draft body (opened before review ran — no `## Evidence`
    table, no review verdict, no Verifiers row). The gate now rebuilds it
    through the SAME `_gather_evidence` -> `_pr_body` ->
    `open_pr(..., update_existing_body=True)` chain `_finalize` uses,
    exercised end to end: `gh pr create` -> "already exists" -> `gh pr list`
    -> `gh pr edit --body <body>`, whose captured body is asserted here.
    """
    branch = "no-human/add-mul"
    # `_run_attempt`'s `pr_branch` shortcut does `repo.checkout(branch)` (no
    # implicit create) — the branch must already exist locally.
    _git(bare_repo, "branch", branch)
    head_sha = _head_sha(bare_repo)

    reviewer = FakeReviewer(_PASSING)
    cfg = _config(tmp_path)
    cfg.data.setdefault("git", {})["github_hosts"] = ["remote.git"]
    events = []
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer,
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    pr_url = "https://github.com/acme/widgets/pull/9"
    t.context = {
        "eval_result": {"verdict": "accept"},
        "pr_branch": branch,
        "pr_draft_created": pr_url,
        "pr_draft_branch": branch,
        # Seeds the Verifiers row: production writes this same key under
        # the reviewed sha; `_gather_evidence` reads it back by that key.
        "verifier_results": {
            head_sha: [{
                "verifier_id": "type_check", "passed": True,
                "evidence": "mypy clean", "file": "calc.py", "line": 1,
                "comment": "", "severity": "info", "files_checked": ["calc.py"],
                "tokens_used": 0, "raw_output": "", "no_verdict": False,
                "unavailable": False,
            }],
        },
    }
    await store.create_task(t)

    gh_calls = _spy_gh_full(monkeypatch, pr_url=pr_url)

    outcome = await orch.run_task(t)
    await store.save_events(t.id, events)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url == pr_url, outcome.pr_url

    edit_calls = [c for c in gh_calls if c[1:3] == ["pr", "edit"]]
    assert len(edit_calls) == 1, gh_calls
    assert edit_calls[0][4] == "--body", edit_calls[0][:5]
    body = edit_calls[0][5]

    assert body.startswith("> **Review passed** (1 round)\n\n## Evidence"), body[:200]
    assert "| Independent review | ✅ **PASSED**" in body, body
    assert "| Verifiers |" in body, body
    # test_evidence=None by design (this path runs no test command) — the
    # Tests row must never claim a pass; _test_evidence_section renders no
    # row at all for a non-dict test_evidence.
    assert "| Tests |" not in body, body


async def test_already_satisfied_posts_the_review_checklist_comment_once(
    bare_repo, tmp_path, store, monkeypatch
):
    """AC2: the reviewer checklist comment is posted exactly once, through
    the SAME `_post_review_checklist_comment` helper `_finalize` uses. A
    second post attempt on the same PR is a no-op (`skipped_duplicate`)."""
    reviewer = FakeReviewer(_PASSING)
    cfg = _config(tmp_path)
    cfg.data.setdefault("git", {})["github_hosts"] = ["remote.git"]
    events = []
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer,
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    pr_url = "https://github.com/acme/widgets/pull/9"
    t.context = {
        "eval_result": {"verdict": "accept"},
        "pr_draft_created": pr_url,
        # Deliberately NOT the run's real (UUID-derived) branch — proves the
        # comment posts regardless of the body-refresh ownership match
        # (that predicate is AC1's concern, not the checklist's).
        "pr_draft_branch": "no-human/add-mul",
    }
    await store.create_task(t)
    _spy_gh_ready(monkeypatch)

    forge: list[str] = []
    monkeypatch.setattr(
        comment_poster, "marker_present_on_pr",
        lambda url, marker: (True, any(marker in b for b in forge)))
    monkeypatch.setattr(
        comment_poster, "post_to_pr",
        lambda url, body: forge.append(body) or
            {"ok": True, "mode": "issue_comment", "error": ""})

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert len(forge) == 1, forge
    assert Orchestrator.REVIEW_CHECKLIST_MARKER in forge[0]

    # A second PASS on the same PR (e.g. a repeat already-satisfied claim)
    # posts nothing further — the same idempotent, marker-based helper.
    result = await orch._post_review_checklist_comment(
        t, pr_url, _PASSING.as_dict(), head_sha="deadbeef", rounds=1)
    assert result is True
    assert len(forge) == 1, forge


async def test_already_satisfied_with_no_pr_posts_nothing_and_opens_none(
    bare_repo, tmp_path, store, monkeypatch
):
    """AC3a: a task with no PR at all (no draft, no watch, no attempt url)
    opens none and posts nothing — no `gh pr create`/`gh pr edit`/`gh pr
    ready` call, and the checklist comment poster is never invoked."""
    reviewer = FakeReviewer(_PASSING)
    cfg = _config(tmp_path)
    cfg.data.setdefault("git", {})["github_hosts"] = ["remote.git"]
    events = []
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer,
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)

    gh_calls = _spy_gh_full(monkeypatch)
    forge: list[str] = []
    monkeypatch.setattr(comment_poster, "marker_present_on_pr",
                        lambda url, marker: (True, False))
    monkeypatch.setattr(
        comment_poster, "post_to_pr",
        lambda url, body: forge.append(body) or
            {"ok": True, "mode": "issue_comment", "error": ""})

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert not outcome.pr_url, outcome.pr_url
    assert gh_calls == [], gh_calls
    assert forge == [], forge


async def test_already_satisfied_does_not_rewrite_a_draft_it_does_not_own(
    bare_repo, tmp_path, store, monkeypatch
):
    """AC3b: a PR this run did not open — surfaced via `pr_watch` (a
    human-linked PR the task is merely tracking, with no `pr_draft_created`
    this run set) — must never have its body rewritten (only the run that
    opened the draft may rewrite it, `vcs/github.py:66`), while the
    checklist comment still posts (additive, safe regardless of ownership).
    """
    branch = "no-human/human-opened"
    _git(bare_repo, "branch", branch)

    reviewer = FakeReviewer(_PASSING)
    cfg = _config(tmp_path)
    cfg.data.setdefault("git", {})["github_hosts"] = ["remote.git"]
    events = []
    orch = Orchestrator(store, cfg.data, AlreadySatisfiedBackend(),
                        SlackNotifier(None), reviewer=reviewer,
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    pr_url = "https://github.com/acme/widgets/pull/9"
    t.context = {
        "eval_result": {"verdict": "accept"},
        "pr_watch": pr_url,
        "pr_branch": branch,
    }
    await store.create_task(t)

    gh_calls = _spy_gh_full(monkeypatch, pr_url=pr_url)
    forge: list[str] = []
    monkeypatch.setattr(comment_poster, "marker_present_on_pr",
                        lambda url, marker: (True, False))
    monkeypatch.setattr(
        comment_poster, "post_to_pr",
        lambda url, body: forge.append(body) or
            {"ok": True, "mode": "issue_comment", "error": ""})

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url == pr_url, outcome.pr_url
    assert not any(c[1:3] == ["pr", "edit"] for c in gh_calls), gh_calls
    assert not any(c[1:3] == ["pr", "create"] for c in gh_calls), gh_calls
    assert len(forge) == 1, forge
