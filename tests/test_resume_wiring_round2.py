"""Round-2 review findings, pinned through `_run_attempt` — not in isolation.

Round 1's wiring test pinned that the checkpoint SHA is recorded, but not the
PRECEDENCE rule that was the actual root cause: with the branch point reverted to
`resume_sha or (wip_sha if attempt_n > 1 else "")`, the whole suite stayed green
(2824 passed). The difference only shows on a task that has BOTH a `resume_from`
and a newer descendant `handoff.wip_sha` — which is precisely the shape of the
live failure (task afe1ed12 was a RESUMED task: 4 attempts, 12,071,981 tokens,
no PR). Round 1's test built a task with no `resume_from`, where the old and new
expressions return the identical value.

Also pinned here, each a defect round 2 demonstrated:

* the `pr_branch` revision path must still credit commits already on the branch —
  narrowing the zero-diff gate to "human-gated resume" made every "LGTM" / CI-fix
  revision that correctly changed nothing fail as fabrication, burning two
  attempts and paging a human;
* a `resume_from` that names a [WIP-PARTIAL] must NOT be credited — the gate must
  key on the commit's SHAPE, because `_checkpoint_wip` returns HEAD when the tree
  is clean, so `resume_from.sha` can name a partial the loop wrote itself;
* an abort's checkpoint write must not clobber `resume_from`, which the CLI writes
  from another process while the attempt runs.
"""
import subprocess

from no_human.agent.claude_backend import AgentResult
from no_human.core.orchestrator import Orchestrator, StuckAbort
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

from .test_e2e_orchestrator import _config, _git, bare_repo  # noqa: F401
from .test_resume_wiring import ScriptedBackend, _ok, _tree  # noqa: F401


def _commit_on_main(work, name: str, body: str, subject: str) -> str:
    """Commit a file onto main and return its sha."""
    (work / name).write_text(body)
    _git(work, "add", "-A")
    _git(work, "commit", "-m", subject)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                          capture_output=True, text=True).stdout.strip()


async def test_attempt_two_prefers_the_newer_partial_over_the_older_resume_point(
        bare_repo, tmp_path, store):
    """THE PRECEDENCE WIRING — the root cause of the 12M-token failure.

    The task was resumed (`resume_from` = a [WIP-BLOCKED] commit), then attempt 1
    left a NEWER [WIP-PARTIAL] that descends from it. Attempt 2 must branch from
    the NEWER one. The pre-fix expression `resume_sha or wip_sha` returns the
    OLDER resume point, so every attempt re-branches from the same commit and
    re-does the same exploration — which is what actually happened live.
    """
    blocked_sha = _commit_on_main(
        bare_repo, "answered.py", "# work a human gated with `nh reply`\n",
        "[WIP-BLOCKED] answer the blocker")
    _git(bare_repo, "push", "origin", "main")

    def attempt1(cwd):
        (cwd / "newer_partial.py").write_text("# tens of turns, AFTER the resume\n")
        raise StuckAbort("doom-loop: identical tool call repeated 3x")

    def attempt2(cwd):
        (cwd / "finished.py").write_text("def done():\n    return True\n")
        return _ok()

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(attempt1, attempt2), SlackNotifier(None))
    t = Task.new("resumed task must not re-branch from the resume point",
                 repo_path=str(bare_repo))
    t.context = {"resume_from": {"sha": blocked_sha, "branch": "main"}}
    await store.create_task(t)
    await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert len(attempts) >= 2, [a["attempt_number"] for a in attempts]
    tree = _tree(bare_repo, attempts[1]["branch_name"])
    assert "newer_partial.py" in tree, (
        "attempt 2 re-branched from the OLDER resume point and discarded "
        f"attempt 1's newer checkpoint: {tree}")


async def test_a_revision_on_an_open_pr_branch_is_not_failed_as_fabrication(
        bare_repo, tmp_path, store):
    """THE REVISION PATH. A PR is open and its branch is ahead of base; a comment
    triggers a revision and the agent correctly changes nothing (a "LGTM, thanks"
    comment, an `nh reject` already addressed, a re-run after a flaky check).

    Narrowing the zero-diff gate to "branched from a human-gated resume" left this
    path with the flag never set, so the attempt was recorded as producing no file
    changes — two burnt attempts and an escalation, which is the exact failure the
    branch-point work exists to remove.
    """
    _git(bare_repo, "checkout", "-b", "nh/existing")
    _commit_on_main(bare_repo, "shipped.py", "def shipped():\n    return 1\n",
                    "shipped work already on the PR branch")
    _git(bare_repo, "push", "-u", "origin", "nh/existing")
    _git(bare_repo, "checkout", "main")

    def changes_nothing(cwd):
        return _ok("Reviewed the comment; the branch already addresses it.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(changes_nothing), SlackNotifier(None))
    t = Task.new("revision that correctly changes nothing",
                 repo_path=str(bare_repo))
    t.context = {"pr_branch": "nh/existing"}
    await store.create_task(t)
    outcome = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert outcome.status is not TaskStatus.ESCALATED, (
        "a revision that correctly changed nothing was escalated to a human: "
        f"{[(a['attempt_number'], a['status'], a['failure_reason']) for a in attempts]}")
    assert not [a for a in attempts
                if (a["failure_reason"] or "").startswith("agent produced no file")], (
        "the revision was failed as fabrication despite real commits on its branch")


async def test_a_human_gated_resume_is_credited_even_when_labelled_wip_partial(
        bare_repo, tmp_path, store):
    """D15. `_checkpoint_wip` returns HEAD when the tree is clean, so the commit a
    blocker records — and therefore the `resume_from` a human's answer writes — is
    ROUTINELY labelled [WIP-PARTIAL]. Judging by that label refuses a genuine
    human-gated resume: the agent correctly adds nothing, is failed for
    "fabrication", and two attempts plus a human page are burned (task 84251cb2).

    `resume_from` is written ONLY by a human action — the reply endpoint,
    `nh reply`, and `nh task resume` (both of which refuse unless the task is
    parked) — so its PROVENANCE, not the commit's subject, is the signal.
    """
    answered = _commit_on_main(
        bare_repo, "answered.py", "def answered():\n    return True\n",
        "[WIP-PARTIAL] work the human then answered a blocker about")
    _git(bare_repo, "push", "origin", "main")
    _git(bare_repo, "branch", "-f", "base-behind", "HEAD~1")
    _git(bare_repo, "push", "origin", "base-behind")

    def changes_nothing(cwd):
        return _ok("Your answer confirms the committed work already satisfies "
                   "every criterion; I will not fabricate an edit.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(changes_nothing), SlackNotifier(None))
    t = Task.new("human-gated resume must be credited", repo_path=str(bare_repo))
    t.context = {
        "resume_from": {"sha": answered, "branch": "main"},
        "base_branch": "base-behind",
    }
    await store.create_task(t)
    outcome = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert outcome.status is not TaskStatus.ESCALATED, (
        "a human-gated resume was refused as fabrication because its checkpoint "
        "carried the [WIP-PARTIAL] label: "
        f"{[(a['attempt_number'], a['status'], a['failure_reason']) for a in attempts]}")
    assert not [a for a in attempts
                if (a["failure_reason"] or "").startswith("agent produced no file")]


async def test_a_revision_branch_sitting_on_an_abandoned_partial_is_not_credited(
        bare_repo, tmp_path, store):
    """The other half. On a reused PR branch nothing records who produced HEAD, and
    every [WIP-PARTIAL] writer commits onto whatever branch the attempt is on —
    including that PR branch. So a revision CAN start on the loop's own abandoned
    partial, and crediting it opens a PR on work no attempt produced while
    `unproductive_streak` never increments. Here the subject IS the only signal.
    """
    _git(bare_repo, "checkout", "-b", "nh/revision")
    _commit_on_main(bare_repo, "half.py", "raise NotImplementedError\n",
                    "[WIP-PARTIAL] abandoned half-work")
    _git(bare_repo, "push", "-u", "origin", "nh/revision")
    _git(bare_repo, "checkout", "main")

    def changes_nothing(cwd):
        return _ok("I reviewed the code and believe it is already complete.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(changes_nothing), SlackNotifier(None))
    t = Task.new("revision sitting on an abandoned partial", repo_path=str(bare_repo))
    t.context = {"pr_branch": "nh/revision"}
    await store.create_task(t)
    await orch.run_task(t)

    final = await store.get_task(t.id)
    attempts = await store.list_attempts(t.id)
    assert not [a for a in attempts if a["status"] == "succeeded"], (
        "an attempt that edited nothing was credited with the abandoned partial "
        f"its PR branch happened to sit on: {[(a['attempt_number'], a['status']) for a in attempts]}")
    assert final.status is not TaskStatus.AWAITING_APPROVAL, (
        "a PR was advanced for work no attempt produced")


async def test_recording_a_checkpoint_does_not_erase_a_concurrent_resume_from(
        bare_repo, tmp_path, store):
    """THE LOST UPDATE. `update_task` rewrites the whole context blob from an
    in-memory Task copy, so it deletes keys another writer added meanwhile — and
    the CLI writes `resume_from` from a DIFFERENT PROCESS (via `merge_context`)
    while an attempt is running. Losing that key loses the human-gated branch
    point the checkpoint machinery exists to protect.
    """
    orch = Orchestrator(store, _config(tmp_path).data, ScriptedBackend(_ok),
                        SlackNotifier(None))
    t = Task.new("concurrent context writers", repo_path=str(bare_repo))
    await store.create_task(t)

    # The orchestrator holds a Task copy from BEFORE the CLI's write.
    stale = await store.get_task(t.id)
    await store.merge_context(t.id, {"resume_from": {"sha": "d" * 40,
                                                     "branch": "dev"}})

    await orch._record_wip_checkpoint(stale, "e" * 40, None,
                                      stopped_because="stuck-abort")

    ctx = (await store.get_task(t.id)).context or {}
    assert (ctx.get("resume_from") or {}).get("sha") == "d" * 40, (
        f"the checkpoint write erased a concurrently-written resume_from: {ctx}")
    assert (ctx.get("handoff") or {}).get("wip_sha") == "e" * 40


async def test_preserving_the_checkpoint_sha_keeps_the_file_list_that_explains_it(
        bare_repo, tmp_path, store):
    """An erroring attempt keeps the PREVIOUS attempt's `wip_sha` so the next one
    does not re-branch from an older commit. Replacing the whole handoff while
    doing so kept the sha but wiped `changed_files`, so the next prompt said
    "READ the files listed above" with nothing listed — the same defect one round
    downstream, on the very path added to prevent it.
    """
    orch = Orchestrator(store, _config(tmp_path).data, ScriptedBackend(_ok),
                        SlackNotifier(None))
    t = Task.new("handoff must not lose the file list", repo_path=str(bare_repo))
    await store.create_task(t)
    await store.merge_context(t.id, {"handoff": {
        "wip_sha": "b" * 40, "changed_files": ["src/b.py"], "summary": "did b.py"}})

    t = await store.get_task(t.id)
    err = AgentResult(final_text="boom", num_turns=3, is_error=True,
                      tokens_used=1, session_id="s", stop_reason="error")
    await orch._persist_handoff(t, err, _NoChangeRepo())

    handoff = ((await store.get_task(t.id)).context or {}).get("handoff") or {}
    assert handoff.get("wip_sha") == "b" * 40, handoff
    assert handoff.get("changed_files") == ["src/b.py"], (
        f"the sha was preserved but the list explaining it was wiped: {handoff}")


class _NoChangeRepo:
    """A repo whose tree is clean and whose HEAD is the preserved checkpoint."""

    def head_sha(self):
        return "b" * 40

    def has_changes(self):
        return False

    def _run(self, *a, **kw):
        return ""


async def test_the_resume_digest_does_not_claim_turns_it_never_counted(
        bare_repo, tmp_path, store):
    """The budget / stuck / timeout aborts have no turn count. The digest asserted
    they "ran out of turns (? used)" — a false statement with a literal "?" —
    injected verbatim into the next attempt's prompt.
    """
    orch = Orchestrator(store, _config(tmp_path).data, ScriptedBackend(_ok),
                        SlackNotifier(None))
    t = Task.new("digest honesty", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch._record_wip_checkpoint(t, "f" * 40, None,
                                      stopped_because="budget exhausted")

    digest = orch._resume_digest(await store.get_task(t.id))
    assert "ran out of turns" not in digest, digest
    assert "?" not in digest.split("left partial work")[0], digest
    assert "budget exhausted" in digest, digest


async def test_the_stuck_abort_path_records_why_it_stopped(bare_repo, tmp_path, store):
    """THE WIRING for the stop reason. A review stripped `stopped_because=` from all
    three abort call sites and the ENTIRE suite stayed green, because the only test
    called the helper directly. This drives the real StuckAbort path instead.

    (The signature now also REQUIRES the argument, so a fourth abort path cannot
    forget it and still import — the test proves the plumbing, the signature
    prevents the regression.)
    """
    def stuck(cwd):
        (cwd / "partial.py").write_text("# work in progress\n")
        raise StuckAbort("doom-loop: identical tool call repeated 3x")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(stuck), SlackNotifier(None))
    t = Task.new("stop reason must be wired", repo_path=str(bare_repo))
    await store.create_task(t)
    await orch.run_task(t)

    handoff = ((await store.get_task(t.id)).context or {}).get("handoff") or {}
    assert handoff.get("stopped_because") == "stuck-abort", (
        f"the abort path did not record why it stopped: {handoff}")
    digest = orch._resume_digest(await store.get_task(t.id))
    assert "ran out of turns" not in digest, digest


async def test_an_abort_with_a_clean_tree_still_records_why_it_stopped(
        bare_repo, tmp_path, store):
    """The early-out on an empty `wip_sha` skipped the stop reason entirely, so an
    abort with nothing uncommitted left the PREVIOUS attempt's `turns_used` in
    place — and the next prompt asserted "ran out of turns (40 used)" about an
    attempt that did nothing of the sort. A confident wrong number is worse than
    the "?" it replaced.
    """
    orch = Orchestrator(store, _config(tmp_path).data, ScriptedBackend(_ok),
                        SlackNotifier(None))
    t = Task.new("clean-tree abort", repo_path=str(bare_repo))
    await store.create_task(t)
    await store.merge_context(t.id, {"handoff": {
        "wip_sha": "a" * 40, "turns_used": 40, "summary": "attempt 1 did X",
        "changed_files": ["src/x.py"]}})

    t = await store.get_task(t.id)
    await orch._record_wip_checkpoint(t, "", None, stopped_because="budget exhausted")

    handoff = ((await store.get_task(t.id)).context or {}).get("handoff") or {}
    assert handoff.get("stopped_because") == "budget exhausted", handoff
    assert handoff.get("turns_used") is None, (
        f"a turn count from an earlier attempt was attributed to this abort: {handoff}")
    assert handoff.get("wip_sha") == "a" * 40, "the older checkpoint must survive"
    digest = orch._resume_digest(await store.get_task(t.id))
    assert "ran out of turns" not in digest, digest
    assert "40" not in digest.split("left partial work")[0], digest


async def test_the_file_list_describes_the_commit_the_handoff_points_at(
        bare_repo, tmp_path, store):
    """`changed_files` was only computed when absent, so after a SECOND abort the
    list still described the EARLIER commit while `wip_sha` had moved on — telling
    the next attempt to read the wrong files.
    """
    orch = Orchestrator(store, _config(tmp_path).data, ScriptedBackend(_ok),
                        SlackNotifier(None))
    t = Task.new("file list must match the commit", repo_path=str(bare_repo))
    await store.create_task(t)
    await store.merge_context(t.id, {"handoff": {
        "wip_sha": "old", "changed_files": ["src/from_an_earlier_commit.py"]}})

    new_sha = _commit_on_main(bare_repo, "actually_changed.py", "x = 1\n",
                              "[WIP-PARTIAL] the newer checkpoint")
    from no_human.vcs.git import GitRepo
    t = await store.get_task(t.id)
    await orch._record_wip_checkpoint(t, new_sha, GitRepo(str(bare_repo)),
                                      stopped_because="stuck-abort")

    handoff = ((await store.get_task(t.id)).context or {}).get("handoff") or {}
    assert handoff.get("changed_files") == ["actually_changed.py"], (
        "the handoff points at a new commit but still lists the previous "
        f"commit's files: {handoff}")


async def test_persisting_a_handoff_does_not_erase_a_concurrent_resume_from(
        bare_repo, tmp_path, store):
    """The sibling of the checkpoint writer. Reverting `_persist_handoff` alone back
    to `update_task` left the suite green — "both writers now use merge_context"
    was only half pinned.
    """
    orch = Orchestrator(store, _config(tmp_path).data, ScriptedBackend(_ok),
                        SlackNotifier(None))
    t = Task.new("persist_handoff must not clobber", repo_path=str(bare_repo))
    await store.create_task(t)

    stale = await store.get_task(t.id)
    await store.merge_context(t.id, {"resume_from": {"sha": "c" * 40,
                                                     "branch": "dev"}})
    err = AgentResult(final_text="boom", num_turns=3, is_error=True,
                      tokens_used=1, session_id="s", stop_reason="error")
    await orch._persist_handoff(stale, err, _NoChangeRepo())

    ctx = (await store.get_task(t.id)).context or {}
    assert (ctx.get("resume_from") or {}).get("sha") == "c" * 40, (
        f"_persist_handoff erased a concurrently-written resume_from: {ctx}")


async def test_a_machine_resume_is_not_credited_as_human_gated(
        bare_repo, tmp_path, store):
    """THE PROVENANCE PREMISE. A commit here once asserted that `resume_from` is
    "written ONLY when a human acted — verified by reading all three writers".
    There are FOUR: `blockers/wake.py` writes it from `WakeWatcher._resume` on
    five autonomous paths (`after:` is a pure timer, `quota_refreshed` fires on a
    clock, plus auto-rebase, CI-fix and gate rungs).

    So a TIMER can put a [WIP-PARTIAL] into `resume_from`, and crediting that
    opens a PR on abandoned half-work no attempt produced while
    `unproductive_streak` never increments.
    """
    partial = _commit_on_main(
        bare_repo, "half.py", "raise NotImplementedError\n",
        "[WIP-PARTIAL] abandoned half-work")
    _git(bare_repo, "push", "origin", "main")
    _git(bare_repo, "branch", "-f", "base-behind", "HEAD~1")
    _git(bare_repo, "push", "origin", "base-behind")

    def changes_nothing(cwd):
        return _ok("I reviewed the code and believe it is already complete.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(changes_nothing), SlackNotifier(None))
    t = Task.new("machine resume must not be credited", repo_path=str(bare_repo))
    # Exactly what wake.py writes: the checkpoint plus its machine provenance.
    t.context = {
        "resume_from": {"sha": partial, "branch": "main", "by": "wake"},
        "resume_reason": "wake_condition_satisfied",
        "base_branch": "base-behind",
    }
    await store.create_task(t)
    await orch.run_task(t)

    final = await store.get_task(t.id)
    attempts = await store.list_attempts(t.id)
    assert not [a for a in attempts if a["status"] == "succeeded"], (
        "an attempt that edited nothing was credited with a [WIP-PARTIAL] that a "
        f"TIMER — not a human — put in resume_from: "
        f"{[(a['attempt_number'], a['status']) for a in attempts]}")
    assert final.status is not TaskStatus.AWAITING_APPROVAL, (
        "a PR was advanced on work no attempt produced")


async def test_a_human_gated_resume_on_a_pr_branch_is_still_credited(
        bare_repo, tmp_path, store):
    """THE REVISION PATH. A task can hold BOTH `pr_branch` and a `resume_from` a
    human wrote. Judging that path by the commit's SHAPE alone — on the stated
    grounds that "nothing records WHO produced this branch's HEAD", while `ctx`
    was in scope the whole time — failed the human-gated resume as fabrication:
    two burnt attempts and a human paged, the D15 regression live on the other
    path.
    """
    _git(bare_repo, "checkout", "-b", "nh/revision")
    head = _commit_on_main(bare_repo, "answered.py", "def answered():\n    return True\n",
                           "[WIP-PARTIAL] work the human then answered about")
    _git(bare_repo, "push", "-u", "origin", "nh/revision")
    _git(bare_repo, "checkout", "main")

    def changes_nothing(cwd):
        return _ok("Your answer confirms the committed work already satisfies it.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(changes_nothing), SlackNotifier(None))
    t = Task.new("human-gated resume on a PR branch", repo_path=str(bare_repo))
    t.context = {"pr_branch": "nh/revision",
                 "resume_from": {"sha": head, "branch": "nh/revision"}}
    await store.create_task(t)
    outcome = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert outcome.status is not TaskStatus.ESCALATED, (
        "a HUMAN-GATED resume was failed as fabrication because it happened to be "
        f"on a PR branch: {[(a['attempt_number'], a['status'], a['failure_reason']) for a in attempts]}")
    assert not [a for a in attempts
                if (a["failure_reason"] or "").startswith("agent produced no file")]


async def test_a_human_resume_after_a_machine_resume_is_still_credited(
        bare_repo, tmp_path, store):
    """THE LATCH. Provenance was read as "is this machine-made?" — and both of its
    signals are ONE-WAY. `wake.py` sets `resume_reason` and nothing ever clears
    it, and `resume_from` merges under RFC 7396, so the human writers (which pass
    `resume_checkpoint(...)` = `{sha, branch}`) inherited a stale `by: "wake"`.

    So EVERY human resume after ANY machine resume was failed as fabrication.
    The trigger is ordinary: a task parks on a wake condition, a TIMER resumes it,
    the attempt raises a blocker, and a human answers with `nh reply`.
    """
    partial = _commit_on_main(
        bare_repo, "answered.py", "def answered():\n    return True\n",
        "[WIP-PARTIAL] work the human then answered about")
    _git(bare_repo, "push", "origin", "main")
    _git(bare_repo, "branch", "-f", "base-behind", "HEAD~1")
    _git(bare_repo, "push", "origin", "base-behind")

    def changes_nothing(cwd):
        return _ok("Your answer confirms the committed work already satisfies it.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(changes_nothing), SlackNotifier(None))
    t = Task.new("human resume after a wake resume", repo_path=str(bare_repo))
    t.context = {
        # The residue of an EARLIER machine resume, which nothing clears…
        "resume_reason": "wake_condition_satisfied",
        # …and the human's answer, written now. It must WIN.
        "resume_from": {"sha": partial, "branch": "main", "by": "human"},
        "base_branch": "base-behind",
    }
    await store.create_task(t)
    outcome = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert outcome.status is not TaskStatus.ESCALATED, (
        "a human resume was refused because the task had been wake-resumed "
        f"earlier in its life: {[(a['attempt_number'], a['status'], a['failure_reason']) for a in attempts]}")


async def test_the_real_writers_stamp_their_own_provenance(bare_repo, tmp_path, store):
    """THE WIRING for the MACHINE writer. Every test above assigns `resume_from`
    literally, so the whole stamp could be deleted from the real writers and the
    suite stayed green — which is exactly how the latch got through.

    🔴 An earlier version of this test claimed to drive "the REAL `nh reply`
    path" and in fact called `store.merge_context` itself — it asserted that a
    dict it had just written contained what it had just put in it, which is true
    of any store and says nothing about any call site. A review caught it as the
    third occurrence of the same mistake. The human writers are pinned through
    their real entry points in `test_api.py` and `test_cli_commands.py`; this
    test owns the machine writer, which lives here.
    """
    from no_human.blockers.wake import WakeWatcher

    t = Task.new("provenance must be stamped by the writers",
                 repo_path=str(bare_repo))
    t.blocker = {"category": "AMBIGUITY", "question": "which?",
                 "resume_commit": "a" * 40, "resume_branch": "main"}
    await store.create_task(t)
    await store.set_status(t, TaskStatus.BLOCKED, validate=False)
    # The residue of an earlier HUMAN resume. Nothing ever clears it, so if the
    # machine's own write is skipped this marker describes the timer's re-entry
    # and the honesty gate credits work no attempt produced — a PR on half-work.
    await store.merge_context(
        t.id, {"resume_from": {"sha": "b" * 40, "branch": "old", "by": "human"}})

    watcher = WakeWatcher(store, _config(tmp_path).data)
    await watcher._resume(await store.get_task(t.id))
    ctx = (await store.get_task(t.id)).context or {}
    assert (ctx.get("resume_from") or {}).get("by") == "wake", (
        f"WakeWatcher._resume did not stamp its own provenance: {ctx.get('resume_from')}")

    # And again with a blocker carrying NO checkpoint — the shape that skipped
    # the write entirely for six review rounds. A timer firing on a task whose
    # blocker recorded no sha is still a MACHINE resume.
    t2 = Task.new("machine resume with no checkpoint", repo_path=str(bare_repo))
    t2.blocker = {"category": "AMBIGUITY", "question": "which?"}
    await store.create_task(t2)
    await store.set_status(t2, TaskStatus.BLOCKED, validate=False)
    await store.merge_context(
        t2.id, {"resume_from": {"sha": "b" * 40, "branch": "old", "by": "human"}})

    await watcher._resume(await store.get_task(t2.id))
    ctx2 = (await store.get_task(t2.id)).context or {}
    assert (ctx2.get("resume_from") or {}).get("by") == "wake", (
        "a machine resume whose blocker held no checkpoint wrote no provenance, "
        f"so a human marker still describes a timer's re-entry: {ctx2.get('resume_from')}")
    assert (ctx2.get("resume_from") or {}).get("sha") is None, (
        "the wake watcher kept a sha it never chose — `by` and `sha` must come "
        f"from the same write: {ctx2.get('resume_from')}")


async def test_a_sha_less_human_reentry_does_NOT_credit_the_loops_own_partial(
        bare_repo, tmp_path, store):
    """THE FAIL-OPEN DIRECTION, end to end through `run_task`.

    🔴 This test exists because eight writer tests that asserted the persisted
    dict were all green while the gate was disarmed. None of them ran
    `_is_own_partial` or the orchestrator afterwards, so the DIRECTION of the
    change was never observed — which is exactly how seven consecutive review
    rounds each fixed one half of this and broke the other.

    The shape: a machine resume (`WakeWatcher`) chooses a `resume_from.sha` that
    happens to be one of the loop's own [WIP-PARTIAL] checkpoints. A human then
    re-enters the loop by a route that names NO checkpoint of its own — `nh
    unblock`, `nh reject`, the drawer's Send back. If that write stamps only
    `by: "human"`, RFC 7396 keeps the machine's sha and relabels it: the gate
    now believes a human gated this branch point, an attempt that edits nothing
    is credited, and a PR is opened on work no attempt produced. That is the
    precise failure this gate exists to prevent, and it is worse than the
    direction it replaced (which merely wasted an attempt).
    """
    from no_human.blockers import resume_provenance

    partial = _commit_on_main(bare_repo, "half.py", "raise NotImplementedError\n",
                              "[WIP-PARTIAL] abandoned half-work")
    _git(bare_repo, "push", "origin", "main")
    # 🔴 THE BASE MUST BE BEHIND THE CHECKPOINT or this test cannot fail.
    # Without it `base` collapses onto the checkpoint, `commits_ahead(base)` is
    # 0, and NOTHING is creditable in either direction — so the `run_task` half
    # passes whatever the gate decides. A review proved that with a three-rung
    # mutation ladder: with the round-6 helper restored the test still passed
    # once its dict assertion was removed, i.e. the end-to-end half was inert
    # and only the dict assertion was doing any work. That is precisely the
    # defect this test was written to stop, reproduced inside the test itself.
    _git(bare_repo, "branch", "-f", "base-behind", "HEAD~1")
    _git(bare_repo, "push", "origin", "base-behind")

    def changes_nothing(cwd):
        return _ok("I reviewed the code and believe it is already complete.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(changes_nothing), SlackNotifier(None))
    t = Task.new("sha-less human re-entry", repo_path=str(bare_repo))
    await store.create_task(t)
    # A MACHINE resume picked this branch point.
    await store.merge_context(t.id, {
        "resume_reason": "wake_condition_satisfied",
        "resume_from": {"sha": partial, "branch": "main", "by": "wake"},
        "base_branch": "base-behind",
    })
    # …then a human re-enters by a route that names no checkpoint. This is the
    # exact value `nh unblock` / `nh reject` / send-back write.
    await store.merge_context(
        t.id, {"resume_from": resume_provenance(None, "human")})

    ctx = (await store.get_task(t.id)).context or {}
    assert (ctx.get("resume_from") or {}).get("sha") is None, (
        "a human re-entry that named no checkpoint kept the sha a MACHINE chose "
        f"and relabelled it: {ctx.get('resume_from')}")

    t = await store.get_task(t.id)
    await orch.run_task(t)

    final = await store.get_task(t.id)
    attempts = await store.list_attempts(t.id)
    assert not [a for a in attempts if a["status"] == "succeeded"], (
        "FAIL-OPEN: an attempt that edited nothing was credited with the loop's "
        "own abandoned partial, because a sha-less human stamp inherited the "
        f"machine's sha: {[(a['attempt_number'], a['status']) for a in attempts]}")
    assert final.status is not TaskStatus.AWAITING_APPROVAL, (
        "a PR was advanced for work no attempt produced")


async def test_a_HUMAN_gated_checkpoint_is_still_credited(bare_repo, tmp_path, store):
    """POSITIVE CONTROL for the test above — without this, "never credit
    anything" would pass it, and that is the D15 regression that burned two
    attempts and paged a human.

    Here the human's own resume NAMES the checkpoint, so `by` and `sha` come
    from the same write and describe the same decision. The attempt correctly
    adds nothing and MUST be credited.
    """
    from no_human.blockers import resume_provenance

    partial = _commit_on_main(bare_repo, "done.py", "print('already done')\n",
                              "[WIP-PARTIAL] work a human then gated")
    _git(bare_repo, "push", "origin", "main")
    # The checkpoint has to be AHEAD of base for there to be anything to credit.
    _git(bare_repo, "branch", "-f", "base-behind", "HEAD~1")
    _git(bare_repo, "push", "origin", "base-behind")

    def changes_nothing(cwd):
        return _ok("The acceptance criteria are already satisfied.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(changes_nothing), SlackNotifier(None))
    t = Task.new("human-gated checkpoint", repo_path=str(bare_repo))
    await store.create_task(t)
    await store.merge_context(t.id, {
        "resume_from": resume_provenance({"sha": partial, "branch": "main"}, "human"),
        "base_branch": "base-behind",
    })

    t = await store.get_task(t.id)
    await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert [a for a in attempts if a["status"] == "succeeded"], (
        "a HUMAN-gated resume whose attempt correctly added nothing was failed "
        f"as fabrication — the D15 regression: {[(a['attempt_number'], a['status'], a['failure_reason']) for a in attempts]}")


# --------------------------------------------------------------------------- #
# D1's side effect: an ORDINARY-subject wake checkpoint is now gated           #
# --------------------------------------------------------------------------- #
#
# 🔴 UNTESTED LIVE BEHAVIOUR CHANGE, flagged by review of the reordered gate.
#
# `_is_own_partial` used to ask the commit's SHAPE first and returned False for
# anything that was not a [WIP-PARTIAL] — so every `by: "wake"` resume onto an
# ordinary commit was CREDITED. The reorder puts provenance first, and
# `wake.py:474` stamps `by: "wake"` on EVERY wake resume from
# `blocker.resume_commit`, which `_checkpoint_wip` fills with plain HEAD
# whenever the tree is clean. Ordinary-subject wake checkpoints are therefore
# routine, not exotic: a `ci_green_on:` or `quota_refreshed` resume where the
# agent correctly adds nothing moved from credited to "file a parseable
# ALREADY-SATISFIED claim or burn the attempt".
#
# The direction is the safe one — a false FAIL, never a false credit, which is
# what the gate is for. But `orchestrator.py:~2458` records an incident of
# exactly this class ("two burnt attempts and a human paged"), so the escape
# hatch is not something to assume: these two tests pin BOTH halves, the cost
# and the escape, on the routine shape rather than on a [WIP-PARTIAL].

def _wake_resume_onto_an_ordinary_commit(bare_repo):
    """The live shape: a wake checkpoint whose subject is an ordinary commit
    message, sitting one commit ahead of the base branch."""
    sha = _commit_on_main(bare_repo, "feature.py", "def feature():\n    return 1\n",
                          "implement the feature")   # NOT [WIP-*] — the point
    _git(bare_repo, "push", "origin", "main")
    # Base must be BEHIND the checkpoint or `commits_ahead(base)` is 0 and
    # nothing is creditable in either direction — the test would be inert.
    _git(bare_repo, "branch", "-f", "base-behind", "HEAD~1")
    _git(bare_repo, "push", "origin", "base-behind")
    return sha


async def test_a_wake_resume_onto_an_ordinary_commit_is_no_longer_credited(
        bare_repo, tmp_path, store):
    """THE COST, stated executably. The agent adds nothing and says so in
    prose. Before the reorder this was recorded `succeeded` on the strength of
    a commit the WAKE WATCHER chose; now the attempt is not credited."""
    sha = _wake_resume_onto_an_ordinary_commit(bare_repo)

    def changes_nothing(cwd):
        return _ok("CI is green now and the change was already committed.")

    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(changes_nothing), SlackNotifier(None))
    t = Task.new("resumed on ci_green_on", repo_path=str(bare_repo))
    t.acceptance_criteria = ["feature() returns 1"]
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)
    await store.merge_context(t.id, {
        "resume_from": {"sha": sha, "branch": "main", "by": "wake"},
        "resume_reason": "wake_condition_satisfied",
        "base_branch": "base-behind",
    })

    t = await store.get_task(t.id)
    final = await orch.run_task(t)

    attempts = await store.list_attempts(t.id)
    assert not [a for a in attempts if a["status"] == "succeeded"], (
        "a MACHINE resume onto an ordinary commit was credited with a diff no "
        f"attempt produced: {[(a['attempt_number'], a['status']) for a in attempts]}")
    assert final.status is not TaskStatus.AWAITING_APPROVAL, (
        "a PR was advanced for work no attempt produced")


async def test_the_already_satisfied_escape_fires_for_that_same_wake_resume(
        bare_repo, tmp_path, store):
    """THE ESCAPE, on the same shape — and the reason the cost above is
    acceptable. An agent that correctly adds nothing and files the cited claim
    must reach the human gate through the fresh-context reviewer, not burn the
    attempt. Without this, "never credit a wake resume" would pass the test
    above, and that is the two-burnt-attempts incident all over again."""
    from .test_e2e_orchestrator import ChecklistItem, FakeReviewer, ReviewDecision

    sha = _wake_resume_onto_an_ordinary_commit(bare_repo)
    claim = ("Re-checked every criterion after CI went green.\n"
             "ALREADY-SATISFIED\n"
             "CRITERION: feature() returns 1 — MET — evidence: feature.py:2\n")

    def files_the_claim(cwd):
        return _ok(claim)

    reviewer = FakeReviewer(ReviewDecision(passed=True, checklist=[
        ChecklistItem("feature() returns 1", True, "feature.py:2 returns 1")]))
    orch = Orchestrator(store, _config(tmp_path).data,
                        ScriptedBackend(files_the_claim), SlackNotifier(None),
                        reviewer=reviewer)
    t = Task.new("resumed on ci_green_on, nothing left to do",
                 repo_path=str(bare_repo))
    t.acceptance_criteria = ["feature() returns 1"]
    # Pin intake: the claim's per-criterion COVERAGE check reads
    # `acceptance_criteria`, and live enrichment would expand it mid-test.
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)
    await store.merge_context(t.id, {
        "resume_from": {"sha": sha, "branch": "main", "by": "wake"},
        "resume_reason": "wake_condition_satisfied",
        "base_branch": "base-behind",
    })

    t = await store.get_task(t.id)
    final = await orch.run_task(t)

    assert [c for c in reviewer.calls if c["mode"] == "already_satisfied"], (
        "the zero-diff escape never reached the reviewer — a correct "
        f"'nothing to add' on a wake resume burns the attempt: {reviewer.calls}")
    assert final.status is TaskStatus.AWAITING_APPROVAL, final.status
    attempts = await store.list_attempts(t.id)
    assert attempts[-1]["status"] == "succeeded", (
        f"{[(a['attempt_number'], a['status'], a['failure_reason']) for a in attempts]}")
