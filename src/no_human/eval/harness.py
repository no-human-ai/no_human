"""Eval harness entrypoints (PLAN.md Part 21): golden-set replay + shadow mode.

``run_eval`` loads the golden set, replays each task in an isolated sandbox,
aggregates a scorecard, and diffs it against the previous run with a CI gate.
``run_shadow`` (21.3) runs a *real* incoming task end-to-end in a sandbox clone
and produces the draft PR locally — without pushing — for human comparison.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .golden import GoldenTask, load_golden_tasks
from .replay import ReplayRunner, TaskScore
from .scorecard import GateResult, Scorecard, ci_gate

CLEANUP_MARKER = ".nh-cleanup-incomplete"


def _remove_sandbox(
    base_tmp: Path, on_event: Callable[[dict], None] | None = None
) -> list[str]:
    """Remove a sandbox we created. Returns the paths ``rmtree`` could not
    remove (empty when everything went).

    Never raises: callers invoke this from a ``finally:`` where an exception
    may already be propagating and cleanup must not mask it. Unlike a bare
    best-effort ``rmtree`` that discards errors outright, a partial removal
    here is recorded — via a marker file and (if given) an event — instead
    of being silently thrown away.
    """
    failed: list[str] = []

    def _record(_func, path, exc) -> None:
        failed.append(f"{path}: {type(exc).__name__}: {exc}")

    try:
        shutil.rmtree(base_tmp, onexc=_record)
    except Exception:
        pass

    # The callback only reports what it saw; a retried/racy removal can still
    # have finished the job, so the filesystem — not the callback log — is
    # the source of truth for whether anything is actually left. A
    # root-level FileNotFoundError (nonexistent target) is therefore success.
    if not base_tmp.exists() or not failed:
        return []

    if len(failed) > 50:
        shown = failed[:50] + [f"... {len(failed) - 50} more"]
    else:
        shown = failed

    marker_text = (
        f"cleanup incomplete at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        + "\n".join(shown) + "\n"
    )
    try:
        (base_tmp / CLEANUP_MARKER).write_text(marker_text, encoding="utf-8")
    except OSError:
        try:
            sibling = base_tmp.parent / (base_tmp.name + ".cleanup-incomplete")
            sibling.write_text(marker_text, encoding="utf-8")
        except OSError:
            pass  # Best-effort record; cleanup must never fail because of it.

    if on_event:
        try:
            on_event({
                "source": "eval", "kind": "sandbox_cleanup_incomplete",
                "text": f"{base_tmp}: {len(failed)} item(s) could not be removed",
            })
        except Exception:
            pass  # A caller-supplied sink must not break cleanup.

    return shown


@dataclass
class EvalRun:
    scorecard: Scorecard
    gate: GateResult
    previous: Scorecard | None = None


async def run_eval(
    config: dict,
    *,
    backend_factory: Callable[[GoldenTask], Any],
    golden_tasks: list[GoldenTask] | None = None,
    reviewer: Any | None = None,
    judge: Any | None = None,
    previous: Scorecard | None = None,
    now: str = "",
    on_event: Callable[[dict], None] | None = None,
    workdir: Path | None = None,
) -> EvalRun:
    """Replay the golden set and produce a gated scorecard."""
    tasks = golden_tasks if golden_tasks is not None else load_golden_tasks()
    runner = ReplayRunner(
        config, backend_factory=backend_factory, reviewer=reviewer,
        judge=judge, on_event=on_event,
    )
    scores: list[TaskScore] = []

    created_tmp = workdir is None
    base_tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="nh-eval-"))
    try:
        for golden in tasks:
            task_dir = base_tmp / golden.id
            task_dir.mkdir(parents=True, exist_ok=True)
            score = await runner.run_one(golden, workdir=task_dir)
            scores.append(score)
            if on_event:
                on_event({"kind": "golden_done", "task": golden.id, "correct": score.correct})

        card = Scorecard(scores=scores, created_at=now)
        gate = ci_gate(card, previous)
        return EvalRun(scorecard=card, gate=gate, previous=previous)
    finally:
        # We own the sandbox only when we created it; a caller-supplied workdir
        # is theirs. Cleanup records what it cannot remove instead of masking
        # it, and still never raises, so it cannot swallow a real error
        # propagating out of this block (0.4).
        if created_tmp:
            _remove_sandbox(base_tmp, on_event)


@dataclass
class ShadowResult:
    task_id: str
    outcome_status: str
    draft_diff: str = ""
    pushed: bool = False  # always False — shadow never pushes
    notes: str = ""


async def run_shadow(
    config: dict,
    *,
    repo_path: str,
    task_title: str,
    backend: Any,
    acceptance_criteria: list[str] | None = None,
    reviewer: Any | None = None,
    workdir: Path | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> ShadowResult:
    """Run a real task end-to-end in a sandbox CLONE — never pushing to the real
    remote (21.3). Produces the draft diff for human comparison."""
    import subprocess

    from ..core.db import Store
    from ..core.orchestrator import Orchestrator
    from ..core.task import Task
    from ..notify.slack import SlackNotifier

    created_tmp = workdir is None
    base_tmp = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="nh-shadow-"))
    sandbox = base_tmp / "clone"
    # Local sandbox copy: no network, isolated inodes, instant on APFS
    # (same class as the bench sandbox — see northstar._sandbox_copy).
    from .northstar import _sandbox_copy
    _sandbox_copy(Path(repo_path), sandbox)
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=sandbox,
                   check=True, capture_output=True)
    subprocess.run(["git", "clean", "-fdx"], cwd=sandbox,
                   check=True, capture_output=True)
    # Push-proof (review F2): the copy carries the source's REAL remotes and
    # the orchestrator pushes at finalize — strip them all and point origin
    # at a throwaway local bare so ShadowResult's "never pushes" is true by
    # construction, not by luck.
    shadow_bare = base_tmp / "shadow-remote.git"
    subprocess.run(["git", "init", "--bare", str(shadow_bare)],
                   check=True, capture_output=True)
    _remotes = subprocess.run(["git", "remote"], cwd=sandbox,
                              capture_output=True, text=True).stdout.split()
    for _r in _remotes:
        subprocess.run(["git", "remote", "remove", _r], cwd=sandbox,
                       capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(shadow_bare)],
                   cwd=sandbox, check=True, capture_output=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=sandbox, capture_output=True, text=True
    ).stdout.strip()

    store = await Store(base_tmp / "shadow.db").connect()
    try:
        orch = Orchestrator(store, config, backend, SlackNotifier(None),
                            reviewer=reviewer, event_sink=on_event)
        task = Task.new(task_title, repo_path=str(sandbox))
        task.acceptance_criteria = list(acceptance_criteria or [])
        await store.create_task(task)
        # B6: neither run_shadow nor the backend bounds a coder TURN by wall
        # clock (only max_turns), so a hung Claude SDK subprocess would wedge
        # the run forever at 0% CPU. Bound the whole task and fail honestly on
        # timeout. Safe to cancel: the sandbox is a throwaway clone dropped in
        # the finally below. Generous default so a legitimately long run is not
        # cut off; override via bounds.shadow_timeout_s.
        timeout_s = float((config.get("bounds") or {}).get("shadow_timeout_s") or 1800)
        try:
            outcome = await asyncio.wait_for(orch.run_task(task), timeout=timeout_s)
        except asyncio.TimeoutError:
            if on_event:
                on_event({"source": "shadow", "kind": "shadow_timeout",
                          "text": f"shadow aborted after {timeout_s:.0f}s — a hung "
                                  "backend turn; failed honestly instead of wedging"})
            diff = subprocess.run(
                ["git", "diff", base_sha, "HEAD"], cwd=sandbox,
                capture_output=True, text=True,
            ).stdout
            return ShadowResult(
                task_id=task.id, outcome_status="timed_out",
                draft_diff=diff, pushed=False,
                notes=f"shadow run timed out after {timeout_s:.0f}s (no coder-turn "
                      "watchdog — B6); the backend subprocess hung, no diff forced.",
            )
        diff = subprocess.run(
            ["git", "diff", base_sha, "HEAD"], cwd=sandbox,
            capture_output=True, text=True,
        ).stdout
        return ShadowResult(
            task_id=task.id, outcome_status=outcome.status.value,
            draft_diff=diff, pushed=False,
            notes="shadow run — clone only, real remote untouched",
        )
    finally:
        # ShadowResult (incl. draft_diff) is already built before this runs, so
        # the clone is safe to drop. Only when we created the tmp (0.4). Nested
        # try/finally so a store.close() failure still lets cleanup run instead
        # of resurrecting the "leak" by skipping it.
        try:
            await store.close()
        finally:
            if created_tmp:
                _remove_sandbox(base_tmp)
