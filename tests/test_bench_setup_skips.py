"""Bench setup failures must be SKIPS, not crashes — issue #424.

Two distinct instrument failures were being booked as agent failures:

1. A pin that no longer resolves in its source repo (history rewritten,
   branch force-pushed, repo re-created) made `run_one` reach
   `git reset --hard <pin>`, which exits 128 and crashes the spec — 56 specs
   observed live, each booked "crashed" with 0 tokens instead of "skipped".
2. A HEAD/empty-pin spec whose SOURCE was itself on a detached HEAD left the
   sandbox detached too (the branch-creation step was pin-gated), so the
   later `git push origin HEAD` failed outright — 13 specs observed live.

The opposite mistake is worse and is guarded for explicitly, mirroring
tests/test_runtime_repo_skip.py: a pin the probe could not VERIFY (no git,
a timeout — `bench_task._pin_reachable` returns `None`) must never skip —
only a CONFIRMED miss (`False`) does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from no_human.eval.bench_task import BenchTask
from no_human.eval.northstar import NorthStarRunner, _sandbox_repo


def _runner() -> NorthStarRunner:
    return NorthStarRunner.__new__(NorthStarRunner)


def _committed_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=a@b",
                    "-c", "user.name=a", "commit", "-qm", "c"], check=True)
    return path


def _head(path: Path) -> str:
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                          capture_output=True, text=True,
                          check=True).stdout.strip()


def _spec(path, pin: str = "", task_id: str = "ns-1") -> BenchTask:
    return BenchTask(id=task_id, title="t", request="r", subset="core",
                     runnable=True,
                     repo={"path": str(path), "pin": pin, "branch": ""})


@pytest.mark.asyncio
async def test_an_unreachable_pin_is_skipped_not_crashed(tmp_path):
    repo = _committed_repo(tmp_path / "repo")
    bogus_pin = "f" * 40
    spec = _spec(repo, pin=bogus_pin)

    score = await _runner().run_one(spec, workdir=tmp_path / "wd")

    assert score.outcome_status == "skipped"
    assert score.goal_satisfied is None      # NOT False — it did not fail
    assert score.nh_tokens == 0
    assert bogus_pin in score.notes, score.notes


@pytest.mark.asyncio
async def test_an_unverifiable_pin_probe_does_not_skip(tmp_path, monkeypatch):
    """`_pin_reachable` returning `None` (probe could not run/complete) is
    NOT the same as a confirmed miss — setup must proceed exactly as today.
    Stubbed here so the test does not depend on a real timeout/missing-git
    condition."""
    import no_human.eval.northstar as ns

    monkeypatch.setattr(ns, "_pin_reachable", lambda repo, pin: None)

    repo = _committed_repo(tmp_path / "repo")
    # Even a pin that would otherwise be confirmed-unreachable must not be
    # skipped while the probe itself cannot answer.
    spec = _spec(repo, pin="f" * 40)

    with pytest.raises(Exception) as exc:
        await _runner().run_one(spec, workdir=tmp_path / "wd")
    # It got past the pin guard — whatever it failed on next (the bare
    # runner has no collaborators wired) is unrelated to the probe.
    assert "is not in" not in str(exc.value)
    assert "pinned commit" not in str(exc.value)


@pytest.mark.asyncio
async def test_a_reachable_pin_is_not_skipped(tmp_path):
    """THE failure mode that is worse than the bug being fixed: a spec whose
    pin DOES resolve must never be skipped by this guard."""
    repo = _committed_repo(tmp_path / "repo")
    spec = _spec(repo, pin=_head(repo))

    with pytest.raises(Exception) as exc:
        await _runner().run_one(spec, workdir=tmp_path / "wd")
    assert "is not in" not in str(exc.value)
    assert "pinned commit" not in str(exc.value)


@pytest.mark.asyncio
async def test_a_floating_pin_never_probes(tmp_path, monkeypatch):
    """HEAD and empty pins measure "whatever the source tip is" — there is
    nothing to probe, so `_pin_reachable` must not even be called."""
    import no_human.eval.northstar as ns

    called = []
    monkeypatch.setattr(
        ns, "_pin_reachable",
        lambda repo, pin: called.append(pin) or True)

    repo = _committed_repo(tmp_path / "repo")
    for pin in ("", "HEAD"):
        with pytest.raises(Exception):
            await _runner().run_one(_spec(repo, pin=pin, task_id=f"ns-{pin}"),
                                    workdir=tmp_path / "wd")
    assert called == []


def test_a_detached_source_still_yields_an_undetached_sandbox(tmp_path):
    """The branch-creation step used to be pin-gated (`if pin != "HEAD"`), so
    a HEAD-pinned source that was ITSELF detached left the sandbox detached
    too, and a later `git push origin HEAD` fails outright ("unable to push
    to unqualified destination"). Proven directly against `_sandbox_repo`,
    the shared push-proof core linked and primary repos both go through."""
    src = _committed_repo(tmp_path / "src")
    subprocess.run(["git", "-C", str(src), "checkout", "-q", "--detach",
                    "HEAD"], check=True)
    assert subprocess.run(
        ["git", "-C", str(src), "symbolic-ref", "-q", "HEAD"],
        capture_output=True).returncode != 0, "fixture must start detached"

    work = _sandbox_repo(src, tmp_path / "work", "HEAD", tmp_path / "bare.git")

    assert subprocess.run(
        ["git", "-C", str(work), "symbolic-ref", "-q", "HEAD"],
        capture_output=True).returncode == 0, "sandbox is still detached"
    # And the push that used to fail now succeeds.
    subprocess.run(["git", "-C", str(work), "push", "origin", "HEAD"],
                   check=True, capture_output=True)


@pytest.mark.asyncio
async def test_floating_pin_scores_a_non_reproducible_note(tmp_path):
    """A HEAD/empty pin measures the source tip on the RUN DAY, not a fixed
    commit — replaying the spec next week replays a different tree. That
    caveat must land in the score's notes wherever notes are written."""
    from no_human.core.task import TaskStatus

    class _FakeOutcome:
        def __init__(self, status):
            self.status = status
            self.detail = ""

    repo = _committed_repo(tmp_path / "repo")
    runner = NorthStarRunner({}, backend_factory=lambda s: None)

    for pin in ("", "HEAD"):
        spec = _spec(repo, pin=pin, task_id=f"ns-{pin or 'empty'}")
        score = await runner._score(
            spec, _FakeOutcome(TaskStatus.FAILED), repo, "HEAD",
            attempts=[], elapsed=1.0)
        assert "non-reproducible" in score.notes, score.notes

    # A pinned (non-floating) spec gets no such caveat.
    pinned = _spec(repo, pin=_head(repo), task_id="ns-pinned")
    score = await runner._score(
        pinned, _FakeOutcome(TaskStatus.FAILED), repo, "HEAD",
        attempts=[], elapsed=1.0)
    assert "non-reproducible" not in score.notes, score.notes


@pytest.mark.asyncio
async def test_a_pin_skip_still_counts_toward_the_unmeasured_gate(tmp_path):
    """Skipped specs must keep leaving the success denominator AND keep
    counting toward the corpus's unmeasured-fraction gate — over-skipping
    must remain visible, not free."""
    from no_human.eval.northstar_card import NorthStarCard, unmeasured_specs

    repo = _committed_repo(tmp_path / "repo")
    spec = _spec(repo, pin="f" * 40)
    score = await _runner().run_one(spec, workdir=tmp_path / "wd")
    assert score.outcome_status == "skipped"

    card = NorthStarCard(scores=[score], label="t")
    assert unmeasured_specs(card) == (1, 1)
