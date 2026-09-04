"""Orchestrator half of the resume-shape repro gate fix.

A resumed attempt (task 51eeaa07: rejected with a "fails-before failed — the
declared repro tests already pass on the base code" error that was actually
the resumed CHECKPOINT masquerading as the base) branches from a
``[WIP-BLOCKED]``/``[WIP-PARTIAL]`` checkpoint, not the task's true base.
``Orchestrator._repro_base_ref``'s old fallback (``HEAD~1`` when neither
``origin/<base>`` nor ``<base>`` resolves — routine in a detached/worktree
checkout) assumed the checkpoint sits exactly one commit above the true
base. When an attempt makes several ordinary commits before being
checkpointed (base -> A(fix) -> B -> checkpoint), ``HEAD~1`` lands on B,
which still carries the fix — so a genuine repro reads as "already passes on
the base".

These tests cover ``_is_resume_shape`` (task-shape detection) and the
resume-aware base-ref resolution that walks past every agent-authored commit
AND every WIP checkpoint (the ordinary work commits, the checkpoints, and —
independently of author identity — anything with a ``[WIP-*]`` subject) to
the true pre-task base, however many attempt commits sit above it.
"""

import json
import subprocess
from pathlib import Path

from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.testing.repro_gate import MANIFEST, run_repro_gate


def _orch(store, tmp_path):
    class _Backend:
        model = "claude-sonnet-5"
        never_push_to = ["main"]

    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


def _git(cwd, *args, author=None):
    env = None
    if author:
        import os
        name, email = author
        env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   text=True, env=env)


def _sha(cwd, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


_HUMAN = ("humandev", "human@example.invalid")
_AGENT = ("no_human", "no-human@acme.com")  # DEFAULT_CONFIG git.agent_identity_*
# A DIFFERENT agent-looking identity than the config default above — the
# shape that exposes the §3 gap: attempt commits authored under an identity
# that does NOT match `_agent_git_identity()`'s configured email.
_AGENT_OTHER_IDENTITY = ("no_human", "no-human@users.noreply.github.com")


def _resumed_multi_commit_repo(tmp_path: Path, *, author=_AGENT) -> dict:
    """base(human, buggy) -> A(agent, fix) -> B(agent, unrelated) ->
    checkpoint(agent, [WIP-BLOCKED]) — HEAD sits on ``checkpoint``, on a
    branch that is NOT named the task's base branch, with no ``origin``, so
    neither ``origin/main`` nor ``main`` resolves (the exact shape that broke
    51eeaa07). Returns the shas so tests can assert which one wins."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "task-branch")
    (work / "calc.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base (buggy)", author=_HUMAN)
    base_sha = _sha(work)

    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "fix add()", author=author)
    fix_sha = _sha(work)

    (work / "README.md").write_text("notes\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "unrelated ordinary commit", author=author)
    b_sha = _sha(work)

    (work / "WIP.md").write_text("checkpoint\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-BLOCKED] paused for a human", author=author)
    checkpoint_sha = _sha(work)

    return {"work": work, "base_sha": base_sha, "fix_sha": fix_sha,
            "b_sha": b_sha, "checkpoint_sha": checkpoint_sha}


# --------------------------------------------------------------------------- #
# _is_resume_shape                                                            #
# --------------------------------------------------------------------------- #


async def test_is_resume_shape_false_for_a_plain_task(tmp_path, store_factory):
    store = await store_factory()
    orch = _orch(store, tmp_path)
    task = Task.new("t", repo_path="/r")
    assert orch._is_resume_shape(task) is False


async def test_is_resume_shape_true_for_explicit_context_flag(tmp_path, store_factory):
    store = await store_factory()
    orch = _orch(store, tmp_path)
    task = Task.new("t", repo_path="/r")
    task.context = {"resume_shape": True}
    assert orch._is_resume_shape(task) is True


async def test_is_resume_shape_true_when_resume_from_sha_present(tmp_path, store_factory):
    store = await store_factory()
    orch = _orch(store, tmp_path)
    task = Task.new("t", repo_path="/r")
    task.context = {"resume_from": {"sha": "a" * 40, "by": "human"}}
    assert orch._is_resume_shape(task) is True


async def test_is_resume_shape_false_on_missing_or_malformed_context(tmp_path, store_factory):
    store = await store_factory()
    orch = _orch(store, tmp_path)
    task = Task.new("t", repo_path="/r")
    task.context = None
    assert orch._is_resume_shape(task) is False
    task.context = {"resume_from": {"sha": ""}}  # present key, empty sha
    assert orch._is_resume_shape(task) is False
    task.context = {"resume_from": "not-a-dict"}
    assert orch._is_resume_shape(task) is False


# --------------------------------------------------------------------------- #
# _repro_base_ref                                                             #
# --------------------------------------------------------------------------- #


async def test_resume_base_walks_past_multiple_attempt_commits(tmp_path, store_factory):
    """AC: multi-commit checkpoint handling. The reviewer's exact scenario: a
    checkpoint that sits THREE commits above the true base. The old
    ``HEAD~1`` fallback would have returned ``b_sha`` (still carrying the
    fix). The fix must walk past every agent-authored commit and land on the
    human-authored base.

    Red-first: this fails against main's pre-fix code, which returns
    ``HEAD~1`` (== ``b_sha``, which already carries the fix) because
    ``_repro_base_ref`` did not accept a ``resume_shape`` kwarg at all.
    """
    store = await store_factory()
    orch = _orch(store, tmp_path)
    shapes = _resumed_multi_commit_repo(tmp_path)
    work = shapes["work"]

    resolved = await orch._repro_base_ref(work, "main", resume_shape=True)

    assert resolved == shapes["base_sha"], (
        f"expected the true base {shapes['base_sha']}, got {resolved!r}")
    assert resolved != shapes["checkpoint_sha"]
    assert resolved != shapes["b_sha"], (
        "landed on HEAD~1 (the ordinary commit still carrying the fix) — "
        "the exact multi-commit-checkpoint bug the reviewer flagged"
    )
    assert resolved != "HEAD~1"


async def test_resume_base_never_returns_a_wip_checkpoint_when_identity_differs(tmp_path, store_factory):
    """§3 gap, red-first against the branch's own (unfixed) walk: the walk
    must never return a [WIP-*] checkpoint as the base, even when the
    attempt's commits are authored under an identity that does not match
    this orchestrator's configured ``git.agent_identity_email``
    (``no-human@acme.com``) — e.g. a different install's identity,
    ``no-human@users.noreply.github.com``. An identity-only walk sees that
    mismatch on its very first step and returns the checkpoint itself,
    reinstating the original bug silently. The WIP-subject check must be
    independent of author identity.
    """
    store = await store_factory()
    orch = _orch(store, tmp_path)
    shapes = _resumed_multi_commit_repo(tmp_path, author=_AGENT_OTHER_IDENTITY)
    work = shapes["work"]

    resolved = await orch._repro_base_ref(work, "main", resume_shape=True)

    assert resolved == shapes["base_sha"], (
        f"expected the true base {shapes['base_sha']}, got {resolved!r}")
    assert resolved != shapes["checkpoint_sha"], (
        "returned the [WIP-BLOCKED] checkpoint itself as the base — "
        "the checkpoint was misread as 'not agent-authored' because the "
        "commit identity did not match the configured agent identity"
    )
    assert resolved != shapes["b_sha"], (
        "returned an ordinary attempt commit as the base — the configured "
        "agent identity did not match the identity actually stamped on "
        "the attempt's own commits, so an identity-only check stops on "
        "the first non-WIP attempt commit it sees"
    )


async def test_all_agent_authored_history_returns_an_unresolvable_base(tmp_path, store_factory):
    """§D — fail-closed: when every reachable commit is agent-authored (no
    human base commit at all), the walk must find nothing and the caller
    must get the unresolvable sentinel — never a guessed sha. Feeding that
    sentinel to ``run_repro_gate`` must read as ``error`` ("cannot verify"),
    never a guessed ``fail``/``pass``.
    """
    store = await store_factory()
    orch = _orch(store, tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "task-branch")
    (work / "calc.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "agent-authored 'base'", author=_AGENT)

    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "fix add()", author=_AGENT)

    (work / "WIP.md").write_text("checkpoint\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "[WIP-BLOCKED] paused for a human", author=_AGENT)

    (work / "test_repro.py").write_text(
        "from calc import add\n\ndef test_add_fixed():\n"
        "    assert add(1, 2) == 3\n"
    )
    (work / ".no_human").mkdir()
    (work / MANIFEST).write_text(
        json.dumps({"tests": ["test_repro.py::test_add_fixed"]})
    )

    resolved = await orch._repro_base_ref(work, "main", resume_shape=True)
    assert resolved == "nh-resume-base-unresolved"

    result = run_repro_gate(work, resolved, resume_shape=True)
    assert result.verdict == "error"
    assert result.verdict not in ("fail", "pass")


async def test_non_resume_fallback_is_still_head_tilde_one(tmp_path, store_factory):
    """AC: no regressions. Criterion 5 for the orchestrator half:
    resume_shape=False (the default) is byte-identical to before this
    feature existed."""
    store = await store_factory()
    orch = _orch(store, tmp_path)
    shapes = _resumed_multi_commit_repo(tmp_path)
    work = shapes["work"]

    resolved = await orch._repro_base_ref(work, "main")

    assert resolved == "HEAD~1"


async def test_merge_base_still_wins_when_base_resolves(tmp_path, store_factory):
    """AC: no regressions. When ``<base>`` actually resolves (here: a local
    branch literally named the base), the ordinary merge-base path wins on
    BOTH ``resume_shape`` values — the resume fallback never fires."""
    store = await store_factory()
    orch = _orch(store, tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    (work / "calc.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base (buggy)", author=_HUMAN)
    base_sha = _sha(work)

    _git(work, "checkout", "-b", "task-branch")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "fix add()", author=_AGENT)

    for resume_shape in (False, True):
        resolved = await orch._repro_base_ref(
            work, "main", resume_shape=resume_shape)
        assert resolved == base_sha, (resume_shape, resolved)


async def test_51eeaa07_shape_end_to_end(tmp_path, store_factory):
    """Fixture reproduction of task 51eeaa07's rejection shape (the live
    task's own rows were not reachable from this sandbox — this is the
    equivalent fixture-based proof named in the plan, not a live rerun).

    Builds the exact resumed shape (checkpoint several commits above the
    true base, no resolvable origin/main) and a real repro manifest/test
    demonstrating the fix. The OLD (non-resume) base-ref path lands on
    ``HEAD~1`` — a commit that already carries the fix — so the gate reads a
    real change as "already passes on the base" and FAILS it. The NEW
    resume-aware path walks to the true base and PASSES the same repro.
    """
    store = await store_factory()
    orch = _orch(store, tmp_path)
    shapes = _resumed_multi_commit_repo(tmp_path)
    work = shapes["work"]

    (work / "test_repro.py").write_text(
        "from calc import add\n\ndef test_add_fixed():\n"
        "    assert add(1, 2) == 3\n"
    )
    (work / ".no_human").mkdir()
    (work / MANIFEST).write_text(
        json.dumps({"tests": ["test_repro.py::test_add_fixed"]})
    )

    old_base_ref = await orch._repro_base_ref(work, "main")
    assert old_base_ref == "HEAD~1"
    old_verdict = run_repro_gate(work, old_base_ref)
    assert old_verdict.verdict == "fail", old_verdict.reasons
    assert "fails-before" in old_verdict.reasons[0]

    new_base_ref = await orch._repro_base_ref(work, "main", resume_shape=True)
    assert new_base_ref == shapes["base_sha"]
    new_verdict = run_repro_gate(work, new_base_ref, resume_shape=True)
    assert new_verdict.verdict == "pass", new_verdict.reasons
    assert new_verdict.resume_shape is True

