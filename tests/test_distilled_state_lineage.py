"""Round-2 review's blocking finding on the distilled-retry mechanism
(task 8b28140d): stale ``distilled_state`` leaking into a resumed attempt 1.

``_distill_attempt_state`` (orchestrator.py) is the only writer of
``task.context['distilled_state']`` / ``['distilled_state_attempt']``, and
``_build_implement_prompt`` is the only consumer. A doc is only ever meant to
feed the SAME attempt that wrote it (``distilled_state_attempt == attempt_n``
at the point of consumption) — a resumed attempt 1 (``nh reply``/requeue
re-enters the bounded loop at ``attempt_n == 1``; attempts are renumbered per
run) must neither consume nor keep a doc a PRIOR run left behind.

This file exercises that lineage guard from both ends: the write-site clear
(``_distill_attempt_state`` at ``attempt_n <= 1``) and the read-site fail-
closed check (``_build_implement_prompt``'s ``ctx_distilled`` block). It does
not touch ``_resume_branch_point``/``handoff``/``resume_from`` — those are a
different, correct mechanism (see ``tests/test_resume_branch_point.py``).
"""

import subprocess

import pytest

from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo_with_diff(tmp_path):
    """A real git repo with a base commit and a second "attempt" commit, the
    same idiom test_attempt_state_distill.py uses for _distill_attempt_state."""
    from no_human.vcs.git import GitRepo

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "base.py").write_text("def base():\n    return 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "checkout", "-b", "task-branch")
    (work / "feature.py").write_text("def feature():\n    return 2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "feature")
    return GitRepo(work)


class _FakeStore:
    def __init__(self):
        self.updates = []

    async def update_task(self, task):
        self.updates.append(task)


def _orch_for_prompt():
    """Minimal orchestrator for `_build_implement_prompt` (the read/consume
    seam) — mirrors test_attempt_state_distill.py's `_orch_for_prompt`."""
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch.ci_runner = None
    orch._active_profile = None
    orch._active_memories = None
    return orch


def _orch_min(store_=None):
    """Minimal orchestrator for `_distill_attempt_state` (the write/clear
    seam)."""
    orch = object.__new__(Orchestrator)
    orch.config = {}
    orch._sink = lambda e: None
    orch.store = store_
    return orch


STALE_MARKER = "STALE-DOC-MARKER from a prior run's attempt 3"


def _leaked_context(produced_by_attempt=3):
    """What a PRIOR run left behind on task.context: a distilled doc tagged
    with that run's own attempt number, never cleared before this run's
    attempt 1 started."""
    return {
        "distilled_state": STALE_MARKER,
        "distilled_state_attempt": produced_by_attempt,
        "gathered": {"chunks": [{"source": "grep", "title": "widget.py hit"}]},
    }


# --------------------------------------------------------------- AC-1 -----

def test_resumed_attempt_one_never_consumes_a_prior_runs_distilled_state(monkeypatch):
    """THE REGRESSION. A resumed run re-enters the bounded loop at
    attempt_n == 1; the seam must never render the stale doc into the
    attempt-1 prompt, no matter what an older run's attempt_seq/attempt_n
    stamp says."""
    import no_human.context.repo_map as rm
    monkeypatch.setattr(rm, "repo_map", lambda p: "MAP-SENTINEL")

    t = Task.new("fix x", repo_path="/tmp/repo")
    t.acceptance_criteria = ["widget renders"]
    t.context = _leaked_context(produced_by_attempt=3)

    orch = _orch_for_prompt()
    prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=1)

    assert STALE_MARKER not in prompt
    assert "DISTILLED STATE FROM YOUR PREVIOUS ATTEMPT" not in prompt
    # attempt 1 re-accumulates, exactly as if no doc existed.
    assert "MAP-SENTINEL" in prompt
    assert "Gathered context" in prompt


# --------------------------------------------------------------- AC-2 -----

def test_retry_attempt_two_still_starts_from_the_distilled_doc(monkeypatch):
    """Positive control: the mechanism this fix must NOT break. A doc
    correctly tagged for attempt 2 IS consumed while building attempt 2's
    prompt. Without this test, a guard hard-wired to always return "" would
    pass the regression test above for the wrong reason."""
    import no_human.context.repo_map as rm
    monkeypatch.setattr(rm, "repo_map", lambda p: "MAP-SENTINEL")

    t = Task.new("fix x", repo_path="/tmp/repo")
    t.acceptance_criteria = ["widget renders"]
    t.context = {
        "distilled_state": "DISTILLED-DOC-FOR-ATTEMPT-2",
        "distilled_state_attempt": 2,
    }
    orch = _orch_for_prompt()
    prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=2)

    assert "DISTILLED-DOC-FOR-ATTEMPT-2" in prompt
    assert "DISTILLED STATE FROM YOUR PREVIOUS ATTEMPT" in prompt
    # the re-accumulated map/digest are replaced, not merely supplemented.
    assert "MAP-SENTINEL" not in prompt
    assert "Gathered context" not in prompt


def test_stamp_from_an_older_attempt_in_the_same_run_is_rejected():
    """The lineage half `attempt_n > 1` alone does not catch: a doc tagged
    for an EARLIER attempt of the SAME run must not be consumed by a LATER
    attempt just because both are > 1."""
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.acceptance_criteria = ["widget renders"]
    t.context = {
        "distilled_state": "DOC-FOR-ATTEMPT-2-NOT-3",
        "distilled_state_attempt": 2,
    }
    orch = _orch_for_prompt()
    prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=3)

    assert "DOC-FOR-ATTEMPT-2-NOT-3" not in prompt
    assert "DISTILLED STATE FROM YOUR PREVIOUS ATTEMPT" not in prompt


@pytest.mark.parametrize("bad_context", [
    {},  # missing key entirely
    {"distilled_state": None, "distilled_state_attempt": 2},
    {"distilled_state": "bare string doc, no lineage tag"},  # legacy shape
    {"distilled_state": "", "distilled_state_attempt": 2},  # empty doc
    {"distilled_state": "doc", "distilled_state_attempt": "2"},  # non-int stamp
    {"distilled_state": "doc", "distilled_state_attempt": None},
], ids=["missing", "none-doc", "legacy-bare-string", "empty-doc",
        "non-int-stamp", "none-stamp"])
def test_guard_fails_closed_on_malformed_records(bad_context):
    t = Task.new("fix x", repo_path="/tmp/repo")
    t.acceptance_criteria = ["widget renders"]
    t.context = bad_context

    orch = _orch_for_prompt()
    # must not raise for any of these shapes, and must never consume them.
    prompt = orch._build_implement_prompt(t, "/tmp/repo", attempt_n=2)
    assert "DISTILLED STATE FROM YOUR PREVIOUS ATTEMPT" not in prompt


# ------------------------------------------------- persistence (real store)

async def test_attempt_one_clears_the_persisted_distilled_state(tmp_path, store):  # noqa: F811
    """The clear must be PERSISTED, not just dropped from the in-memory
    Task, and it must be SCOPED: other context keys (handoff, resume_from)
    written by the checkpoint machinery survive it untouched."""
    t = Task.new("fix x", repo_path=str(tmp_path))
    await store.create_task(t)
    await store.merge_context(t.id, {
        "distilled_state": STALE_MARKER,
        "distilled_state_attempt": 3,
        "handoff": {"wip_sha": "a" * 40, "changed_files": ["src/a.py"]},
        "resume_from": {"sha": "b" * 40, "branch": "dev"},
    })
    t = await store.get_task(t.id)

    orch = _orch_min(store)
    repo = _repo_with_diff(tmp_path)
    await orch._distill_attempt_state(t, repo, 1, "main")

    reloaded = (await store.get_task(t.id)).context or {}
    assert "distilled_state" not in reloaded
    assert "distilled_state_attempt" not in reloaded
    assert reloaded.get("handoff", {}).get("wip_sha") == "a" * 40
    assert reloaded.get("resume_from", {}).get("sha") == "b" * 40


async def test_write_site_stamps_the_lineage(tmp_path, store):  # noqa: F811
    """Observes the persisted artifact (MEMORY: tests must observe
    artifacts, not recompute the expectation from the code under test): after
    the post-write, the stamp on the RELOADED record equals the attempt that
    produced it."""
    t = Task.new("fix x", repo_path=str(tmp_path))
    t.context = {"attempt_log": ["attempt 1: failed"]}
    await store.create_task(t)
    t = await store.get_task(t.id)

    orch = _orch_min(store)
    repo = _repo_with_diff(tmp_path)
    await orch._distill_attempt_state(t, repo, 2, "main")

    reloaded = (await store.get_task(t.id)).context or {}
    assert reloaded.get("distilled_state_attempt") == 2
    assert reloaded.get("distilled_state")
