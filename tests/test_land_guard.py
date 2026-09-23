"""Every land blocks on a report-only history scan it cannot fail.

`nh approve`'s land pushes from the MAIN checkout (`approve_merge.py` step 7),
never a worktree — see `_land_in_worktree`'s step-7 comment. If that checkout
carries its own `core.hooksPath` pre-push hook (unrelated to `push_hook.py`,
which only ever installs into a WORKTREE's `core.hooksPath` for the agent's
task-branch pushes), a plain `git push` there runs it INLINE and the land
waits for it. When `NH_GUARD_MODE` is unset/"report" that hook cannot refuse
the push — its only possible effect is a log line — so that wait buys the
land nothing; it is pure wall-clock sitting on the critical path of every
approve.

`land_guard.py` fixes this: in report mode, with a hook actually present, the
push goes through with `--no-verify` and the identical hook is immediately
re-run out of band (a detached child the land never waits on). Anything else
— an unrecognised/enforcing `NH_GUARD_MODE`, no hook at all, the `
NH_LAND_GUARD_DEFER=0` escape hatch — runs the hook synchronously, exactly as
`git push` always has.

These tests drive REAL git against the REAL `land_env` fixture (imported from
`tests.test_approve_merge`, which already stands `land_env.clone` in for a
task's `repo_path` — the "main checkout" in land terms) with a REAL
executable `pre-push` hook installed via `core.hooksPath`, so what is being
proven is what git itself does with `--no-verify` and `core.hooksPath`, not
a mocked stand-in for it.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from no_human.vcs import land_guard
from no_human.vcs.approve_merge import land_task
from no_human.vcs.git import GitRepo
from no_human.vcs.push_hook import hook_dir_for

from tests.test_approve_merge import (
    LandEnv,
    _git,
    _patch_run_pytest,
    land_env,
)

# Silence "imported but unused" — these are fixtures/helpers re-exported for
# pytest's collection and for the test bodies below.
assert LandEnv and land_env


def _install_guard_hook(clone, marker: Path, *, sleep_s: float = 0,
                         refuse: bool = False) -> Path:
    """Install a REAL executable `pre-push` hook into *clone* via
    `core.hooksPath` — standing in for the private-checkout history-scan
    hook this repo does not carry. Every invocation appends one line to
    *marker* (proving the hook ran) and the full stdin it was handed
    (`<marker>.stdin`, proving a deferred re-run reproduces the pre-push
    protocol) before sleeping *sleep_s* seconds and exiting 1 (refuse) or 0
    (report-only pass) depending on *refuse*."""
    hooks_dir = clone / ".git" / "guard-hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-push"
    marker_q = shlex.quote(str(marker))
    stdin_marker_q = shlex.quote(str(marker) + ".stdin")
    hook.write_text(
        "#!/bin/sh\n"
        f"cat >> {stdin_marker_q}\n"
        f"echo ran >> {marker_q}\n"
        f"sleep {sleep_s}\n"
        f"exit {1 if refuse else 0}\n"
    )
    hook.chmod(0o755)
    _git(clone, "config", "core.hooksPath", str(hooks_dir))
    return hook


def _wait_for(predicate, *, timeout=10.0, interval=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _land(land_env, monkeypatch, *, branch: str) -> "object":
    """Runs `land_task` for an ALREADY-CUT branch. The branch must be cut
    (and its own `git push -u origin <branch>` from `land_env.clone`
    completed) BEFORE any guard hook is installed on that clone — otherwise
    an enforce/refuse hook would block the fixture's own setup push, not
    just the land's step-7 push these tests are targeting."""
    _patch_run_pytest(monkeypatch, returncode=0)
    return land_task(
        repo_path=str(land_env.clone), branch=branch, pr_url=land_env.pr_url,
        task_id="deadbeef", task_title="Add feature",
        review_evidence="review PASS", config=land_env.config,
    )


def test_report_mode_land_does_not_run_the_gate_inside_the_push(
    land_env, monkeypatch,
):
    """The whole bug: report mode cannot refuse, so a slow scan must not sit
    on the land's critical path. A 3s hook must not add anywhere near 3s to
    `land_task`'s own wall-clock."""
    monkeypatch.delenv("NH_GUARD_MODE", raising=False)
    branch, _head_sha = land_env.cut_branch("no-human/t-defer-fast")
    marker = land_env.tmp_path / "marker.txt"
    _install_guard_hook(land_env.clone, marker, sleep_s=3)

    start = time.monotonic()
    result = _land(land_env, monkeypatch, branch=branch)
    elapsed = time.monotonic() - start

    assert result.ok, result.stderr
    assert elapsed < 2.0, (
        f"land_task took {elapsed:.2f}s with a 3s pre-push hook installed — "
        "the report-only gate ran (or was waited on) inside the push")


def test_report_mode_runs_the_gate_out_of_band_after_the_push(
    land_env, monkeypatch,
):
    """The deferred hook must actually run, out of band, over the pushed
    range — a report-mode gate that silently never runs again is just a
    quieter version of the same bug (nothing ever gets scanned)."""
    monkeypatch.delenv("NH_GUARD_MODE", raising=False)
    branch, _head_sha = land_env.cut_branch("no-human/t-defer-runs")
    marker = land_env.tmp_path / "marker.txt"
    _install_guard_hook(land_env.clone, marker, sleep_s=0)

    result = _land(land_env, monkeypatch, branch=branch)
    assert result.ok, result.stderr
    assert not marker.exists(), (
        "hook already ran synchronously — this test's premise (a deferred "
        "gate) does not hold")

    assert _wait_for(marker.exists, timeout=10.0), (
        "the deferred gate never ran the hook at all")
    stdin_marker = Path(str(marker) + ".stdin")
    assert _wait_for(stdin_marker.exists, timeout=10.0)
    stdin_text = stdin_marker.read_text()
    assert result.landed_sha in stdin_text, (
        f"deferred hook stdin {stdin_text!r} does not name the landed sha "
        f"{result.landed_sha}")
    assert "deferred" in result.guard_note.lower()


def test_enforce_mode_gate_can_still_refuse_the_land(land_env, monkeypatch):
    """MUST fail under an unconditional `git push --no-verify` fix: enforce
    mode's whole point is that the gate can still block a land."""
    branch, _head_sha = land_env.cut_branch("no-human/t-enforce-refuse")
    monkeypatch.setenv("NH_GUARD_MODE", "enforce")
    marker = land_env.tmp_path / "marker.txt"
    _install_guard_hook(land_env.clone, marker, refuse=True)
    before = land_env.remote_main_sha()

    result = _land(land_env, monkeypatch, branch=branch)

    assert not result.ok, (
        "land succeeded despite an enforce-mode hook refusing the push — "
        "looks like an unconditional --no-verify")
    assert result.step == "push"
    assert marker.exists(), "the hook never even ran"
    after = land_env.remote_main_sha()
    assert after == before, "main advanced on the remote despite the refusal"


def test_enforce_mode_gate_that_passes_still_lands(land_env, monkeypatch):
    branch, _head_sha = land_env.cut_branch("no-human/t-enforce-pass")
    monkeypatch.setenv("NH_GUARD_MODE", "enforce")
    marker = land_env.tmp_path / "marker.txt"
    _install_guard_hook(land_env.clone, marker, refuse=False)

    result = _land(land_env, monkeypatch, branch=branch)

    assert result.ok, result.stderr
    # Enforce mode always runs synchronously -- the marker must already be
    # there the instant land_task returns, not merely "eventually".
    assert marker.exists()


def test_unknown_guard_mode_is_treated_as_enforcing(land_env, monkeypatch):
    """An unrecognised value is safety's default: run the gate synchronously
    (never silently treat a typo/future value as report-and-defer)."""
    branch, _head_sha = land_env.cut_branch("no-human/t-unknown-mode")
    monkeypatch.setenv("NH_GUARD_MODE", "some-future-mode-nobody-wrote-yet")
    marker = land_env.tmp_path / "marker.txt"
    _install_guard_hook(land_env.clone, marker, refuse=True)
    before = land_env.remote_main_sha()

    result = _land(land_env, monkeypatch, branch=branch)

    assert not result.ok
    assert result.step == "push"
    after = land_env.remote_main_sha()
    assert after == before


def test_land_without_any_hook_is_unchanged(land_env, monkeypatch):
    """No `core.hooksPath` hook at all: a plain push, no --no-verify, no
    deferred child, nothing new to observe."""
    monkeypatch.delenv("NH_GUARD_MODE", raising=False)
    branch, _head_sha = land_env.cut_branch("no-human/t-no-hook")
    result = _land(land_env, monkeypatch, branch=branch)
    assert result.ok, result.stderr
    assert "no pre-push hook" in result.guard_note


def test_land_push_comment_names_the_main_checkout_hooks_path(land_env):
    """The comment above step 7's push claimed 'the main repo carries no
    such hook' -- true only of push_hook.py, never of an operator's own
    core.hooksPath. Pin the correction so it cannot silently regress."""
    src = Path(land_guard.__file__).parent.joinpath("approve_merge.py").read_text(
        encoding="utf-8")
    assert "the main repo carries no such hook" not in src, (
        "the misleading claim is still there verbatim")
    assert "core.hooksPath" in src


def test_worktree_keeps_its_own_hooks_path_and_agent_push_is_untouched(
    tmp_path,
):
    """Per-worktree `core.hooksPath` isolation (push_hook.py, pre-existing
    and out of scope here) must keep resolving independently of whatever the
    primary checkout's own `core.hooksPath` is set to: `land_guard`, pointed
    at a worktree, must resolve that worktree's OWN generated guard hook
    file, never the main checkout's hook file directly (push_hook.py
    deliberately DELEGATES to whatever hook was previously effective, so the
    main checkout's hook still fires as a step inside the worktree's own
    hook -- see test_existing_repository_hooks_still_fire_in_the_agent_
    worktree in test_push_hook_guard.py -- but that is delegation, not the
    same hook object, and land_guard.py is never even invoked on a worktree
    push: `approve_merge.py` step 7 only ever calls `plan_push`/
    `resolve_pre_push_hook` on the MAIN checkout's `repo.path`)."""
    remote = tmp_path / "remote.git"
    up = tmp_path / "upstream"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "README.md").write_text("hello\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "init")
    _git(up, "remote", "add", "origin", str(remote))
    _git(up, "push", "-q", "origin", "main")

    main_marker = tmp_path / "main-marker.txt"
    _install_guard_hook(up, main_marker, sleep_s=0)

    wt_path = tmp_path / "agent-wt"
    main_repo = GitRepo(up, never_push_to=["main", "master", "release/*"])
    main_repo.add_worktree(wt_path, base="main", detach=True)

    wt_hook = land_guard.resolve_pre_push_hook(wt_path)
    main_hook = land_guard.resolve_pre_push_hook(up)

    assert main_hook == (up / ".git" / "guard-hooks" / "pre-push").resolve()
    assert wt_hook == (hook_dir_for(wt_path) / "pre-push").resolve()
    assert wt_hook != main_hook, (
        "the worktree resolved the main checkout's guard hook instead of "
        "its own push_hook.py-installed one")

    # And the agent's own push path -- untouched by any of this, since
    # land_guard is never in it -- still works exactly as
    # tests/test_push_hook_guard.py already pins: a legitimate task-branch
    # push succeeds, and the pre-existing hook still fires (by DELEGATION,
    # not because land_guard resolved it as the same object -- asserted
    # above).
    _git(wt_path, "checkout", "-q", "-b", "no-human/task-1")
    (wt_path / "README.md").write_text("hello\nagent\n")
    _git(wt_path, "commit", "-qam", "agent work")
    proc = _git(wt_path, "push", "-u", "origin", "no-human/task-1", check=False)
    assert proc.returncode == 0, f"agent task-branch push was blocked: {proc.stderr}"
    assert main_marker.exists(), (
        "push_hook.py's documented delegation to a pre-existing hook did "
        "not fire -- premise of this test no longer holds")
