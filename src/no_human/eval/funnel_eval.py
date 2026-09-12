"""The nightly funnel eval (Phase C, tasks C3 + C4).

Drives the five-tier funnel corpus through the REAL Orchestrator in a fresh
temp-HOME instance, scores each tier against its measured criteria (C1), writes
a dated report plus a summary, and holds the night against a ratchet (C4).

WHAT THIS IS AFRAID OF, and what it does about it — each answered in code
below, not in this docstring:

* **A hung backend wedging the night.** Every tier runs under a HARD wall clock
  (its own ``max_wall_seconds``), and the whole run under the sum of them. A
  tier past its wall is CANCELLED and RECORDED with ``stage`` ==
  ``wall_clock_kill``; the remaining tiers still run. Subprocesses the runner
  starts itself (the held-out test) are killed by PROCESS GROUP, the same
  ``start_new_session`` + ``killpg`` shape ``northstar._holdout_ok`` and
  ``testing/runner._kill_process_tree`` use — a plain ``proc.kill()`` leaves the
  pytest children alive.

  HONEST LIMIT of the per-tier wall, stated here rather than discovered at
  03:00: it is ``asyncio.wait_for``, so it interrupts a tier that is AWAITING
  something. A tier wedged inside a blocking call on a worker thread (an
  ``asyncio.to_thread`` that never returns, a subprocess the ORCHESTRATOR
  started and does not bound) cannot be cancelled by it and will outlast its
  ceiling. The outer bound for that case is ``scripts/nightly_eval.sh``, which
  kills the whole process group and exits 124. Two bounds, not one, because
  neither alone covers both shapes of hang.

* **A corpus that quietly measures less than it claims to.** A run refuses
  unless the corpus is exactly ``EXPECTED_TIERS``, by NAME. Without that an
  emptied ``eval/funnel_corpus/`` reports "0 passed, 0 failed" and exits 0 —
  the instrument broken and the night green.

* **A review gate that is not there.** The default path builds the same
  ``AdversarialReviewer`` ``nh bench`` builds. It deliberately does NOT set
  ``reviewer.allow_advisory``: that makes a SKIPPED gate return ``passed=True``,
  which turns every tier's ``review_passed`` criterion into a rubber stamp. A
  night with no reviewer must be RED, and there is a test that it is.
* **A crash reading as a green night.** Every failure path returns nonzero. The
  runner has no branch that returns 0 without a verdict for every tier.
* **Corrupting the operator's day instance.** The run gets its own HOME, its own
  database, its own worktree root and its own port, and asserts all four are
  inside the temp HOME before anything is materialized.
* **Spending more than the night is worth.** The runner REFUSES to start when
  the corpus's ceiling sum exceeds ``eval.nightly_budget_tokens``. The check is
  first, so a refusal has actually refused something.

The backend arrives through ``backend_factory`` — the same injection seam
``eval.northstar.NorthStarRunner``, ``eval.replay.ReplayRunner`` and
``eval.harness.run_eval`` take, so the tests never monkeypatch the SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..core.db import USAGE_ROLES, Store, usage_columns_for
from ..core.orchestrator import Orchestrator
from ..core.task import Task
from ..notify.slack import SlackNotifier
from ..core.pricing import BUDGET_UNIT_KEY, WEIGHTED_UNIT
from .funnel_corpus import (
    CORPUS_DIR, EXPECTED_TIERS, CorpusTask, corpus_ceiling_tokens, load_corpus,
)
from .funnel_criteria import evaluate

log = logging.getLogger(__name__)

BASELINE_PATH = CORPUS_DIR / "baseline.json"

#: Deliberately not 8420 (``server.port``'s default, the operator's day
#: instance). Nothing here binds a socket — the port lands in the run's config
#: so that if a future step does start one, it cannot land on the operator's.
NIGHTLY_PORT = 8431

#: How far over its baseline cost a tier may drift before the night is red.
#: A band, not a cap: token counts are noisy at n=1 and a 1% wobble that fails
#: a nightly run is a gate people learn to ignore.
COST_BAND = 1.25

#: Bound on the held-out test subprocess, copied from
#: ``northstar._holdout_ok`` so the two cannot drift.
HOLDOUT_TIMEOUT_S = 300


# --------------------------------------------------------------------------- #
# The instance                                                                 #
# --------------------------------------------------------------------------- #

def _instance_config(home: Path, base: dict | None = None) -> dict:
    """A config for a run that shares nothing with the operator's instance."""
    from ..config import load_config

    home.mkdir(parents=True, exist_ok=True)
    data = json.loads(json.dumps(base)) if base else load_config(
        home / "config.yaml").data
    data.setdefault("server", {})["port"] = NIGHTLY_PORT
    data.setdefault("database", {})["path"] = str(home / "no_human.db")
    data.setdefault("isolation", {})["worktree_root"] = str(home / "worktrees")
    # The pre-split alias is still read by `config.worktree_root`; leaving it
    # pointing at ~/.no_human/worktrees would put the night's worktrees in the
    # operator's tree for any config written before the split.
    data.setdefault("concurrency", {})["worktree_root"] = str(home / "worktrees")
    # A nightly run is unattended: nobody reads a 03:00 ping, and a webhook
    # inherited from a supplied config would page a human for a corpus tier.
    for key in ("slack_webhook_url", "teams_webhook_url"):
        data.setdefault("notifications", {})[key] = None
    return data


def _default_backend_factory(data: dict) -> Callable[[CorpusTask], Any]:
    """The coder backend the shipped nightly path uses, built like `nh bench`
    builds it (cli/commands.py:5317-5322) and sourced from the run's OWN
    instance config.

    ``model`` is keyword-ONLY and has no default (claude_backend.py:442-445),
    so the obvious `ClaudeBackend()` is a TypeError before the coder starts —
    five crashed tiers a night, forever, for a reason that is not a
    regression. Nothing caught it because every test injects a factory.

    The other two kwargs are the ones `nh bench` passes and they are passed
    here for the same reasons: ``forbidden_paths`` arms the PreToolUse guard
    (the real safety boundary, Part 10), and ``never_push_to`` is constraint
    #2 — the corpus's origin is a workdir-local bare, but the guard must hold
    because it is armed, not because the remote happens to be harmless.

    Deliberately NOT passed, exactly as `nh bench` does not pass them:
    ``permission_mode`` (``bypassPermissions`` is already the unattended
    default), ``readonly`` (False is right — this one writes), and
    ``supervisor_hook`` / ``lint_hook`` / ``tool_result_caps``, which the
    Orchestrator wires itself per task and which a factory here would only
    be able to get wrong.
    """
    from ..agent.claude_backend import ClaudeBackend

    def factory(_task: CorpusTask) -> Any:
        return ClaudeBackend(
            model=data["llm"]["primary_model"],
            forbidden_paths=data["safety"]["forbidden_paths"],
            never_push_to=data["git"]["never_push_to"],
        )

    return factory


def _assert_isolated(home: Path, data: dict) -> None:
    from ..config import NO_HUMAN_HOME

    home = home.resolve()
    for key, path in (("database.path", data["database"]["path"]),
                      ("isolation.worktree_root",
                       data["isolation"]["worktree_root"])):
        p = Path(path).resolve()
        if not p.is_relative_to(home):
            raise RuntimeError(f"{key} escapes the nightly HOME: {p}")
        if p.is_relative_to(NO_HUMAN_HOME.resolve()):
            raise RuntimeError(
                f"{key} is inside the operator's ~/.no_human: {p}")


# --------------------------------------------------------------------------- #
# One tier                                                                     #
# --------------------------------------------------------------------------- #

def _holdout_ok(task: CorpusTask, work: Path) -> bool | None:
    """Run this tier's held-out test against whatever the agent left behind.

    Same bounded mechanics as ``northstar._holdout_ok`` / ``replay._mergeable``:
    a new session so the whole pytest process TREE can be killed on timeout,
    and a timeout is a RED holdout, never a hang.
    """
    if not task.holdout_cmd:
        return None
    proc = subprocess.Popen(
        task.holdout_cmd, cwd=work, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
        # DONTWRITEBYTECODE: the holdout imports the tier's package from the
        # work tree, and without this every run leaves __pycache__ behind in
        # a checkout somebody then has to read `git status` on.
        env={**os.environ, "PYTHONPATH": str(work),
             "PYTHONDONTWRITEBYTECODE": "1"},
        start_new_session=True)
    try:
        proc.communicate(timeout=HOLDOUT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        return False
    return proc.returncode == 0


def _checkout_pr_branch(outcome: Any, work: Path) -> None:
    """Put the work dir on the coder's PR branch before the holdout runs.

    The orchestrator commits the coder's work on that branch and can leave the
    work-dir HEAD at base, so a holdout run against HEAD would measure the
    UNTOUCHED repo and score every real PR red — the false-empty-view defect
    ``northstar._agent_diff_ref`` records. Forced, for the same reason it is
    forced there: the deliverable is committed on the branch, so uncommitted
    sandbox cruft is not part of the PR.
    """
    ctx = getattr(getattr(outcome, "task", None), "context", None) or {}
    branch = ctx.get("pr_branch")
    if not branch:
        return
    for cand in (branch, f"origin/{branch}"):
        if subprocess.run(["git", "rev-parse", "--verify", cand], cwd=work,
                          capture_output=True).returncode == 0:
            subprocess.run(["git", "checkout", "-f", cand], cwd=work,
                           capture_output=True)
            return
    log.warning("nightly: pr_branch %r not resolvable in %s", branch, work)


def _spend(attempts: list[dict]) -> dict[str, int]:
    """Every registered role, per price class — the role list is IMPORTED, never
    enumerated here (``northstar._score``'s ``_class_total``: a role in the
    schema but not in the sum is spend the benchmark hands the product free)."""
    def total(idx: int) -> int:
        keys = [usage_columns_for(t)[idx] for t in USAGE_ROLES]
        return sum(int(a.get(k) or 0) for a in attempts for k in keys)

    return {"tokens_used": total(0), "cache_read_tokens": total(1),
            "cache_creation_tokens": total(2)}


async def _run_tier(task: CorpusTask, home: Path, data: dict,
                    backend_factory: Callable[[CorpusTask], Any],
                    reviewer: Any | None) -> dict:
    """Drive one tier to terminal and return its record. Never raises."""
    record: dict[str, Any] = {"task": task.name, "stage": "setup",
                              "detail": "", "pr_url": "", "review_passed": False,
                              "holdout": None, "wall_seconds": 0.0,
                              "weighted_tokens": None}
    work: Path | None = None
    t0 = time.monotonic()
    try:
        work = task.materialize(home / "work")
        backend = backend_factory(task)
        store = await Store(home / "no_human.db").connect()
        try:
            nh_task = Task.new(task.ticket["title"], repo_path=str(work),
                               description=task.ticket["description"])
            nh_task.acceptance_criteria = list(
                task.ticket.get("acceptance_criteria") or [])
            # THE tier's ceiling, filed as the task's REAL cap. Without this
            # the only thing bounding a runaway tier is `bounds.lifetime_tokens`
            # — 4M for every task alike — so five tiers authorise a 20M night
            # while the doc calls the corpus sum (10.9M) the authorised
            # ceiling. The static pre-check below compares ceilings pinned
            # equal to their own sum and can never fire on its own; this is
            # what actually stops the spend, per tier, at the number the tier
            # published.
            #
            # STAMPED. `pricing.config_is_weighted` fails closed: an unmarked
            # cap is read as a pre-cutover RAW number and converted DOWN by
            # ~5x, which would silently hand t1 an 80k budget instead of 400k.
            # These numbers are weighted — the same unit `FunnelCriteria`
            # compares and `weighted_tokens` prices — so say so.
            nh_task.config = {
                **(nh_task.config or {}),
                "lifetime_tokens": task.criteria.max_weighted_tokens,
                BUDGET_UNIT_KEY: WEIGHTED_UNIT,
            }
            await store.create_task(nh_task)
            orch = Orchestrator(store, data, backend, SlackNotifier(None),
                                reviewer=reviewer)
            record["stage"] = "running"
            try:
                outcome = await asyncio.wait_for(
                    orch.run_task(nh_task),
                    timeout=task.criteria.max_wall_seconds)
            except asyncio.TimeoutError:
                record["stage"] = "wall_clock_kill"
                record["detail"] = (
                    f"killed after {task.criteria.max_wall_seconds}s — the tier "
                    "exceeded its own wall-clock ceiling")
                record["wall_seconds"] = time.monotonic() - t0
                record.update(_spend(await store.list_attempts(nh_task.id)))
                record["weighted_tokens"] = None
                return record
            record["wall_seconds"] = time.monotonic() - t0
            record["stage"] = outcome.status.value
            record["detail"] = outcome.detail or ""
            record["pr_url"] = outcome.pr_url or ""
            attempts = await store.list_attempts(nh_task.id)
            record.update(_spend(attempts))
            record["weighted_tokens"] = None
            record["review_passed"] = any(
                int(a.get("review_passed") or 0) for a in attempts)
        finally:
            await store.close()
        await asyncio.to_thread(_checkout_pr_branch, outcome, work)
        record["holdout"] = await asyncio.to_thread(_holdout_ok, task, work)
    except Exception as exc:  # noqa: BLE001 — fail-closed, never a green night
        log.exception("nightly: tier %s crashed", task.name)
        record["stage"] = "crashed"
        record["detail"] = f"{type(exc).__name__}: {exc}"
        record["wall_seconds"] = time.monotonic() - t0
    return record


# --------------------------------------------------------------------------- #
# C4 — the ratchet                                                             #
# --------------------------------------------------------------------------- #

def load_baseline(path: Path | None = None) -> dict:
    p = Path(path or BASELINE_PATH)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A missing or unreadable baseline is UNSEEDED, not "everything passes".
        return {"unseeded": True, "tasks": []}


def compare_to_baseline(records: list[dict], baseline: dict) -> tuple[bool, list[str]]:
    """Hold tonight's records against the recorded baseline.

    Returns ``(ratchet_holds, lines)``. Two blocking movements: a tier that
    PASSED at baseline and failed tonight, and a tier that cost more than
    ``COST_BAND`` times its baseline cost. Both name the task and the numbers,
    because a ratchet line somebody has to go and re-derive is a ratchet line
    somebody ignores.

    An improvement is reported and CHANGES NOTHING. This function never writes
    the baseline: a run that tightens its own floor turns one lucky night into
    a gate nobody can pass, which is the "bench is an instrument, not a target"
    failure in its purest form. Refreshing is a human editing the file in the
    commit that fixes or accepts something.
    """
    if baseline.get("unseeded"):
        return True, [
            "baseline is UNSEEDED — no ratchet yet, report only. Seed it from "
            "the first real run you have read (see `_how_to_refresh` in "
            "eval/funnel_corpus/baseline.json)."]

    prior = {t["task"]: t for t in baseline.get("tasks", [])}
    lines: list[str] = []
    ok = True
    for rec in records:
        was = prior.get(rec["task"])
        if was is None:
            lines.append(f"new tier {rec['task']} — not in the baseline, "
                         f"not ratcheted")
            continue
        if was.get("passed") and not rec.get("passed"):
            ok = False
            lines.append(
                f"REGRESSION {rec['task']}: passed at baseline "
                f"({baseline.get('recorded', 'unknown date')}), failed tonight")
        band = int(was.get("cost", 0) * COST_BAND)
        cost = int(rec.get("cost") or 0)
        if cost > band:
            ok = False
            lines.append(
                f"COST {rec['task']}: {cost:,} > {band:,} "
                f"(baseline {int(was.get('cost', 0)):,} + "
                f"{int((COST_BAND - 1) * 100)}% band)")
        elif cost and cost < int(was.get("cost", 0)):
            lines.append(
                f"improved {rec['task']}: {cost:,} vs baseline "
                f"{int(was.get('cost', 0)):,} — recorded, NOT ratcheted; "
                f"the baseline is refreshed by a human, never by a run")
    for name in sorted(set(prior) - {r["task"] for r in records}):
        lines.append(f"missing {name}: in the baseline, not in tonight's run")
        ok = False
    return ok, lines


# --------------------------------------------------------------------------- #
# The night                                                                    #
# --------------------------------------------------------------------------- #

def _summary(report: dict) -> str:
    rows = ["| tier | verdict | stage | quality | cost | wall |",
            "| --- | --- | --- | --- | --- | --- |"]
    for t in report["tasks"]:
        rows.append(
            f"| {t['task']} | {'PASS' if t['passed'] else 'FAIL'} | "
            f"{t['stage']} | {t['quality']} | {t['cost']:,} | "
            f"{t['wall_seconds']:.0f}s |")
    failures = [f"- **{t['task']}** ({t['stage']}): " + "; ".join(t["failures"])
                for t in report["tasks"] if not t["passed"]]
    return "\n".join([
        f"# Nightly funnel eval — {report['date']}",
        "",
        f"**{report['passed']} passed, {report['failed']} failed** "
        f"(exit {report['exit_code']}).",
        "",
        *([f"> **REFUSED**: {report['refused']}", ""] if report.get("refused")
          else []),
        *rows,
        "",
        "## Failures",
        *(failures or ["- none"]),
        "",
        "## Ratchet",
        *[f"- {ln}" for ln in report["ratchet"]],
        "",
        "Quality is measured by each tier's held-out test. There is no score "
        "here and there must never be one.",
        "",
    ])


def _write(out: Path, report: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / f"nightly-{report['date']}.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "SUMMARY.md").write_text(_summary(report), encoding="utf-8")


def _corpus_refusal(tasks: list[CorpusTask], *, defaulted: bool) -> str | None:
    """Why this corpus cannot be run, or None.

    Names, never a count: five copies of the cheapest tier satisfy a count and
    measure a fifth of what the night claims. A caller that supplies its own
    corpus (a test, a one-tier reproduction) is only held to "not empty" — but
    empty is checked on BOTH paths, because ``0 passed, 0 failed, exit 0`` is
    the exact shape of a night that ran nothing and reported health.
    """
    names = [t.name for t in tasks]
    if not names:
        return ("the corpus is EMPTY — 0 tiers to run. A night that measured "
                "nothing is not a night that passed")
    if not defaulted:
        return None
    missing = sorted(set(EXPECTED_TIERS) - set(names))
    unexpected = sorted(set(names) - set(EXPECTED_TIERS))
    if missing or unexpected:
        return ("the corpus is not the five expected tiers — "
                + (f"MISSING {', '.join(missing)}" if missing else "")
                + ("; " if missing and unexpected else "")
                + (f"UNEXPECTED {', '.join(unexpected)}" if unexpected else "")
                + ". A corpus that lost a tier measures less than this run "
                  "claims to")
    return None


async def _run(home: Path, out: Path, *, backend_factory, reviewer,
               corpus: list[CorpusTask] | None, config: dict | None,
               nightly_budget_tokens: int | None) -> int:
    from ..config import DEFAULT_CONFIG

    tasks = corpus if corpus is not None else load_corpus()
    data = _instance_config(home, config)
    _assert_isolated(home, data)

    # BOTH defaults are built HERE, from `data`, and never from the raw
    # `config` argument. They used to be resolved in `run_funnel_eval` against
    # `config or DEFAULT_CONFIG`: the same value today, divergent by
    # construction — editing the nightly HOME's `config.yaml` moved the model
    # the planner and coder run on and left the reviewer on the default, with
    # nothing anywhere reporting the split. One source, so they cannot drift.
    if backend_factory is None:
        backend_factory = _default_backend_factory(data)
    if reviewer is _UNSET:
        # Exactly what `nh bench` constructs (cli/commands.py:5332). Left as
        # None the orchestrator raises ReviewerUnavailable AFTER the coder
        # attempt is paid for — five red tiers a night for a reason that reads
        # like a product regression. `reviewer.allow_advisory` is NOT the fix:
        # it makes a skipped gate report a pass.
        from ..review.reviewer import AdversarialReviewer
        reviewer = AdversarialReviewer.from_config(data)

    report: dict[str, Any] = {
        "date": date.today().isoformat(),
        "instance": {"home": str(home), "db_path": data["database"]["path"],
                     "worktree_root": data["isolation"]["worktree_root"],
                     "workdir": str(home / "work"), "port": data["server"]["port"]},
        "tasks": [], "passed": 0, "failed": 0, "ratchet": [], "exit_code": 1,
    }

    # BEFORE the budget check and before anything is materialized: a refusal
    # that fires after three tiers have run has not refused anything.
    refusal = _corpus_refusal(tasks, defaulted=corpus is None)
    if refusal:
        report["refused"] = refusal
        report["ratchet"] = ["not evaluated: the run refused to start"]
        _write(out, report)
        return 1

    budget = int(nightly_budget_tokens if nightly_budget_tokens is not None
                 else (data.get("eval") or {}).get(
                     "nightly_budget_tokens",
                     DEFAULT_CONFIG["eval"]["nightly_budget_tokens"]))
    ceiling = corpus_ceiling_tokens(tasks)
    if ceiling > budget:
        report["refused"] = (
            f"corpus ceiling {ceiling:,} weighted tokens exceeds "
            f"eval.nightly_budget_tokens {budget:,} — nothing was run")
        report["ratchet"] = ["not evaluated: the run refused to start"]
        _write(out, report)
        return 1

    deadline = time.monotonic() + sum(t.criteria.max_wall_seconds for t in tasks)
    for task in tasks:
        if time.monotonic() >= deadline:
            rec = {"task": task.name, "stage": "run_deadline_exceeded",
                   "detail": "the run's total wall-clock budget was spent "
                             "before this tier started",
                   "pr_url": "", "review_passed": False, "holdout": None,
                   "wall_seconds": 0.0, "weighted_tokens": None}
        else:
            rec = await _run_tier(task, home, data, backend_factory, reviewer)
        verdict = evaluate(rec, task.criteria)
        rec.update(passed=verdict.passed, failures=verdict.failures,
                   cost=verdict.cost, quality=verdict.quality)
        report["tasks"].append(rec)

    report["passed"] = sum(1 for t in report["tasks"] if t["passed"])
    report["failed"] = len(report["tasks"]) - report["passed"]
    ratchet_ok, report["ratchet"] = compare_to_baseline(
        report["tasks"], load_baseline())
    report["exit_code"] = 0 if (report["failed"] == 0 and ratchet_ok) else 1
    _write(out, report)
    return report["exit_code"]


#: "not supplied" — distinct from an explicit ``reviewer=None``, which means
#: "run with NO review gate" and must stay expressible: the test that a tier
#: cannot pass without a reviewer needs to ask for exactly that.
_UNSET = object()


def run_funnel_eval(home: Path, out: Path, *, backend_factory=None,
                    reviewer=_UNSET, corpus: list[CorpusTask] | None = None,
                    config: dict | None = None,
                    nightly_budget_tokens: int | None = None) -> int:
    """Run tonight's funnel eval. Returns the process exit code: 0 only if
    every tier passed AND the ratchet held.

    Fail-closed at the outermost edge too: a crash in the runner itself is a
    RED night, never a missing one.
    """
    # Both defaults are resolved inside `_run`, against the run's own instance
    # config — `backend_factory=None` and `reviewer=_UNSET` travel there as
    # "not supplied". Nothing is constructed here.
    try:
        return asyncio.run(_run(
            Path(home), Path(out), backend_factory=backend_factory,
            reviewer=reviewer, corpus=corpus, config=config,
            nightly_budget_tokens=nightly_budget_tokens))
    except Exception as exc:  # noqa: BLE001
        log.exception("nightly funnel eval crashed")
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / "SUMMARY.md").write_text(
            f"# Nightly funnel eval — {date.today().isoformat()}\n\n"
            f"**CRASHED**: {type(exc).__name__}: {exc}\n\n"
            "A crash is a red night, not a missing one.\n", encoding="utf-8")
        return 1


def main(argv: list[str] | None = None) -> int:
    """`python -m no_human.eval.funnel_eval` — what scripts/nightly_eval.sh and
    the launchd job run. The exit code IS the verdict."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="nightly-funnel-eval",
        description="Run the nightly funnel-regression eval. Exit 0 only if "
                    "every tier passed and the ratchet held.")
    ap.add_argument("--home", default="~/.no_human-nightly",
                    help="the run's OWN no_human home — never the operator's")
    ap.add_argument("--out", default="", help="default: <home>/out")
    args = ap.parse_args(argv)
    home = Path(args.home).expanduser()
    return run_funnel_eval(home, Path(args.out).expanduser() if args.out
                           else home / "out")


__all__ = ["run_funnel_eval", "compare_to_baseline", "load_baseline",
           "corpus_ceiling_tokens", "BASELINE_PATH", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
