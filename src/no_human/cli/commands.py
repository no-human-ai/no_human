"""`nh` command-line interface (PLAN.md Part 6).

Phase 0 runs the orchestrator synchronously in-process (no daemon yet — that is
Phase 4). `nh task add` runs a task end-to-end with live streaming; `nh watch`
runs a staged task inside the Textual TUI.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import print_path_error, stdio_is_interactive
from .. import __version__
from ..agent.claude_backend import ClaudeBackend
from ..agent.backend import make_backend, resolve_backend_name, SUPPORTED_BACKENDS
from ..config import (
    AuthError,
    _windows_pid_alive,
    assert_codex_mode,
    assert_local_backend_mode,
    assert_subscription_mode,
    codex_auth_mode,
    load_config,
)
from ..context import ContextGatherer, build_default_sources
from ..core.db import USAGE_ROLES, Store
from ..core.events import EventPersister
from ..core.lanes import status_buckets
from ..core.orchestrator import CODER_ROLE, Orchestrator, is_agent_session
from ..core.runtime import build_orchestrator
from ..core import slot_wait
from ..core.slot_wait import is_waiting_for_slot
from ..core.task import (PRIORITY_ORDER, Task, TaskStatus,
                          normalise_priority)
from ..intake import (
    classify_kind,
    ingest_from_url,
    is_plain_text_task,
    kind_criteria_mismatch,
    parse_source,
)
from ..notify import build_notifier
from ..vcs.task_pr import PR_EVENT_KINDS, task_has_pr_evidence
# Re-exported, not just used: `nh status` calls `_probe_pool` through THIS
# module's globals, and that is the name the CLI tests monkeypatch to keep
# themselves off the dev box's real 127.0.0.1:8420 listener.
from .pool_probe import (  # noqa: F401
    POOL_BAD_BODY, POOL_HTTP_ERROR, POOL_LIVE, POOL_NO_SCHEDULER, POOL_REFUSED,
    POOL_TIMEOUT, POOL_UNREACHABLE, PROBE_TIMEOUT_S, PoolProbe, _pool_note,
    _probe_pool,
)

console = Console()

# Read the platform through a constant, never an inline `os.name` test, so the
# Windows branches below are reachable from a test on any host — no Windows
# machine or runner is available to this project.
_IS_WINDOWS = os.name == "nt"


def print_no_task_matching(task_id: str) -> None:
    """Print the task-not-found error with a remediation hint.

    ``task_id`` is user-supplied and may contain rich markup characters
    (brackets); it is always escaped so it renders literally.
    """
    console.print(f"[red]no task matching[/] {escape(str(task_id))}")
    console.print("Fix: run 'nh task list' to see task ids (a unique id prefix is enough).")


def _server_owns_worker(config) -> bool:
    """True when an `nh start` server is up, and therefore owns the worker pool.

    Its scheduler claims every PENDING or IMPLEMENTING task (scheduler.py
    ``_CLAIMABLE``). A CLI command that ALSO runs the task in-process gives one
    task two orchestrators driving the same git checkout — two coders, two
    reviewers, two commits, and potentially two PRs. Observed on task 84251cb2:
    duplicate `commit`/`reviewing` events and a doubled escalation.

    A failure of the HTTP probe alone is not treated as "no server": `nh serve`
    binds no socket, so its scheduler is invisible to this probe even while
    running. The pidfile `nh serve` and `nh start` both take (`_acquire_pid_lock`)
    is the second, socket-free channel, and `_pidfile_owner_alive` is checked as
    a fallback. The cost of a false negative there is the old behavior, while a
    false positive would silently strand the task.
    """
    import json as _json
    import urllib.error
    import urllib.request

    srv = config.get("server", {}) or {}
    host = srv.get("host", "127.0.0.1")
    port = srv.get("port", 8420)
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/api/tasks", timeout=1.5
        ) as resp:
            if resp.status != 200:
                return False
            _json.loads(resp.read() or b"null")  # it really is our API
            return True
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return _pidfile_owner_alive()


def _post_server_cancel(config, task_id: str, reason: str) -> bool:
    """POST /api/tasks/{task_id}/cancel to the running server, so `nh task
    cancel` hard-stops a live coder session through the exact same path the
    board's cancel button uses (`cancel_task` → `Scheduler.request_task_cancel`
    → `Orchestrator.request_task_cancel` cancelling the coder's backend.run
    task directly) instead of only ever raising the cooperative DB flag below,
    which a backend that never emits an SDK event would never notice.

    Returns True on any 2xx response. False — never raises past here — for
    anything short of that, including the case `_server_owns_worker`'s
    docstring already calls out: `nh serve` binds no HTTP socket, so a caller
    that got here via the pidfile fallback has no endpoint to reach at all.
    The caller falls back to the pre-existing cooperative-only message.
    """
    import json as _json
    import urllib.error
    import urllib.request

    srv = config.get("server", {}) or {}
    host = srv.get("host", "127.0.0.1")
    port = srv.get("port", 8420)
    body = _json.dumps({"reason": reason}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{port}/api/tasks/{task_id}/cancel",
        data=body, method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


def _pidfile_owner_alive() -> bool:
    """True when NO_HUMAN_HOME/nh.pid names a process that is alive.

    `nh serve` binds no socket, so the HTTP probe above cannot see it. Since
    serve now takes the same pid lock `nh start` does, the lock file IS the
    second, socket-free channel. Missing, unreadable, non-integer or dead pid
    → False, preserving the "any doubt means no server" bias.
    """
    from ..config import NO_HUMAN_HOME
    try:
        pid = int((NO_HUMAN_HOME / "nh.pid").read_text().strip())
    except (OSError, ValueError):
        return False
    # `_probe_pid` is tri-state (True/False/None-for-another-user's-pid); only
    # a confirmed-alive PID counts here, unlike `_acquire_pid_lock` where None
    # means "not ours to reason about, take the lock" — the safe answer for a
    # probe is "assume no server", not "assume it's ours".
    return _probe_pid(pid) is True


def _running_pool_stats(config) -> tuple[int | None, int, dict | None] | None:
    """The (busy, width) of the pool that is actually draining the queue, or
    None when there is nothing to ask.

    `nh status` used to print `working N/{config max_workers}`. Under
    `nh start --workers N` — a flag that deliberately leaves the config on disk
    untouched — that denominator was the number nobody was running, and
    saturation is the one thing an operator reads this line for. It also used
    to take the NUMERATOR from a count of worker-owned status rows
    (IMPLEMENTING among them), which is not evidence of a running worker: a
    row can sit in IMPLEMENTING after a restart strands it, claimable and
    waiting, with nothing spending on it — `scheduler._ORPHANABLE` deliberately
    excludes IMPLEMENTING for exactly that reason. That printed impossible
    ratios like `working 8/4`.

    `/api/queue/health` reports the live `Scheduler.max_workers` AND
    `workers_busy` (`core/health.py`'s `len(inflight_ids)`, the scheduler's
    actual in-flight set, which cannot exceed `max_workers`), so it is the
    only honest source for both numbers while a server is up. Same discipline
    as `_server_owns_worker` above: any failure to reach it, any non-JSON
    answer, and a reported width below 1 (a server with no scheduler attached,
    which is not a running pool) all mean "no live stats" — the caller then
    says so instead of passing a config number off as an observation.

    Returns `(busy, width, pause)` when reachable, where `pause` is
    `{"reason", "until", "profile"}` from the same `/api/queue/health`
    payload's `paused_*` fields when `paused` is true, else `None` — so a
    quota-cooldown pool prints why it isn't draining instead of a bare
    `working 0/N` next to an ETA computed as if work were flowing. `busy` is
    `int(payload["workers_busy"])` when that key is present and parses to a
    non-negative int, and `None` when the key is absent or unparseable —
    `busy is None` means "the endpoint answered but did not report a
    numerator" (an older build, say), not "unreachable": the caller keeps
    counting rows for the numerator in that case, while still trusting the
    observed denominator, so a partial payload doesn't print a false
    `working 0/N` on a busy pool.

    KNOWN GAP — this covers the app/api server case only. `nh start` is what
    puts a scheduler behind an HTTP server; `serve()` below runs the scheduler
    in a bare asyncio loop and binds NO socket, so under `nh serve
    --max-workers N` there is nothing to ask and status falls back to the
    config number. The fallback is labelled, not silent, but it is blind: it
    can print an impossible-looking ratio such as `working 3/2 (configured;
    server not running)` while a 3-wide serve pool is in fact draining. Fixing
    that needs `serve` to expose the width somewhere a second process can read
    (a status endpoint or a pid-file field), which is not this change.

    This is a thin wrapper over `pool_probe._probe_pool`, which also carries
    WHY there are no stats (`PoolProbe.outcome`) — a timeout, an HTTP 500 and
    a connection refused are three different facts, and `nh status` uses that
    distinction to choose its words; this function keeps the pre-existing
    `(busy, width, pause) | None` contract for callers (like `task_show`) that
    only need the stats.
    """
    return _probe_pool(config).stats


def _local_hhmm(iso: str | None) -> str:
    """ISO timestamp -> local 24-hour `HH:MM`, mirroring `formatPausedUntil`
    in `web/src/drainChip.js` so the CLI and the board print the same resume
    time. A naive (no-tzinfo) ISO string is assumed UTC before converting to
    local time. Unparseable or absent input returns "unknown time" — the
    same words the board uses for the same case."""
    from datetime import datetime, timezone
    if not iso:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "unknown time"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%H:%M")


def _bootstrap(*, require_auth: bool = True):
    """Load config + enforce subscription mode. Returns (config, scrub_report)."""
    config = load_config()
    report = None
    if require_auth:
        try:
            # `or {}`: config.yaml is hand-edited, and a bare `llm:` with its
            # body commented out deep-merges to None, not to a dict.
            _llm = config.get("llm") or {}
            report = assert_subscription_mode(
                profile=_llm.get("auth_profile"),
                auth_mode=_llm.get("auth_mode", "subscription"),
            )
            # The Anthropic assertion above still runs unconditionally, even
            # when the CODER is Codex: the reviewer, planner, supervisor and
            # utility tiers stay on Claude — the review gate and the four
            # model tiers are fixed by constraint — so an install
            # that dropped the Claude credential would lose its review gate and
            # discover it one task later. Codex adds a SECOND per-vendor
            # assertion; it never replaces the first.
            if resolve_backend_name(config.data) == "codex":
                assert_codex_mode(
                    codex_auth_mode(config.data),
                    cli_path=_llm.get("codex_cli_path"),
                )
            # Same rule as codex: the Anthropic assertion above still runs,
            # because the reviewer/planner/supervisor/utility tiers stay on
            # Claude regardless of what the coder runs on.
            if resolve_backend_name(config.data) == "local":
                assert_local_backend_mode((_llm or {}).get("local_base_url"))
        except AuthError as exc:
            console.print(f"[bold red]auth error:[/] {exc}")
            # The Codex failure carries its own complete remedy — either "add
            # OPENAI_API_KEY" (api_key mode) or "run `codex login`"
            # (subscription mode) — and both raisers name
            # `llm.codex_auth_mode` in their message. Appending the Claude
            # recipe under either would send the operator to `claude
            # setup-token` for a problem that has nothing to do with their
            # Claude token.
            if "llm.codex_auth_mode" in str(exc):
                sys.exit(2)
            console.print(
                "\n[bold]Fix:[/] run [bold]nh init[/] to set up authentication, or:\n"
                "  1. [bold]claude setup-token[/]  (creates a subscription token)\n"
                "  2. Add it to ~/.no_human/.env:\n"
                "     [bold]echo 'CLAUDE_CODE_OAUTH_TOKEN=<token>' >> ~/.no_human/.env[/]\n"
                "  3. If ANTHROPIC_API_KEY is set, unset it:\n"
                "     [bold]unset ANTHROPIC_API_KEY[/]"
            )
            sys.exit(2)
    return config, report


def _refuse_agent_gate_act(act: str) -> None:
    """The act-level half of the human gate (session_mark.py): refuse `act`
    outright if THIS process carries the agent-session mark, before any
    other work — including `_bootstrap`'s config load or opening the
    `Store` — happens. Additive to `guard.py`'s existing lexical PreToolUse
    checks, which stay untouched; this is the checkpoint that still catches
    a caller that dodges those by spelling the command differently.

    Exit code 2 separates this from the exit 1 used for ordinary business
    refusals ("no passing review yet"), but it is NOT unique to it: Click's
    own UsageError also exits 2 (`nh approve` with no TASK_ID does). A caller
    must key on the printed `refused: ... marked agent session`, not the code.
    """
    from ..agent.session_mark import GateRefused, refuse_if_marked

    try:
        refuse_if_marked(act)
    except GateRefused as exc:
        console.print(f"[bold red]refused:[/] {exc.reason}")
        sys.exit(2)


def _assert_backend_usable() -> None:
    """Refuse to start the server when the coding backend can't run a task.

    A present OAuth token is necessary but not sufficient: the Claude Agent SDK
    shells out to the ``claude`` CLI for every task, and a fresh install can have
    a valid token yet no CLI on PATH. Without this the board renders green and
    EVERY task then dies at launch with CLINotFoundError — a silent cliff. The
    token half is already enforced by ``assert_subscription_mode``; this closes
    the CLI half. Blocking and loud, exactly where the operator will see it.
    """
    from ..agent.backend_check import find_claude_cli

    if find_claude_cli() is None:
        console.print(
            "[bold red]coding backend unavailable:[/] the `claude` CLI was not "
            "found.\n"
            "The Claude Agent SDK shells out to it for every task, so the board "
            "would load but every task would fail at launch.\n\n"
            "[bold]Fix:[/] install the Claude Code CLI, then restart:\n"
            "  [bold]npm install -g @anthropic-ai/claude-code[/]\n"
            "Verify with [bold]nh doctor[/] (it now checks this)."
        )
        sys.exit(2)
    # The `claude` CLI above is required even when the CODER is Codex, because
    # the reviewer, planner, supervisor and utility tiers stay on Claude. When
    # Codex is selected there is a SECOND binary with the same all-or-nothing
    # property, and the same silent cliff if it is missing.
    #
    # Signature deliberately unchanged (no parameters): both call sites pass
    # none, and a test double is `lambda: None`. Config is re-read here rather
    # than threaded through.
    cfg = load_config()
    if resolve_backend_name(cfg.data) == "codex":
        from ..agent.codex_backend import find_codex_cli

        if find_codex_cli((cfg.get("llm") or {}).get("codex_cli_path")) is None:
            console.print(
                "[bold red]coding backend unavailable:[/] worker.backend is "
                "'codex' but the `codex` CLI was not found.\n"
                "Every coder task would fail at launch.\n\n"
                "[bold]Fix:[/] install it, then restart:\n"
                "  [bold]npm install -g @openai/codex[/]\n"
                "…or set [bold]worker.backend: claude[/] in ~/.no_human/config.yaml."
            )
            sys.exit(2)


def _warn_if_editable_install_dangles() -> None:
    """Loud, never fatal: a dangling _editable_impl_no_human.pth (left behind
    when a coder worktree the shared venv was editable-installed against gets
    garbage-collected) makes the checkout read as corrupted. Warning, not a
    gate — never exits, never affects `nh doctor`'s pass/fail.
    """
    try:
        from ..doctor import editable_install_problem

        problem = editable_install_problem()
    except Exception:  # noqa: BLE001 — a diagnostic must never block startup
        return
    if problem:
        console.print(f"[bold yellow]⚠ {problem}[/]")


def _ensure_board_fresh() -> None:
    """Loud, never fatal (see core/web_build): detects a `web/dist` built
    before the latest `web/src` change on a source checkout and rebuilds it
    (or warns) before the board is mounted.
    """
    try:
        from ..core.web_build import ensure_fresh_board
        ensure_fresh_board(emit=lambda m: console.print(f"[bold yellow]⚠ {m}[/]"))
    except Exception:  # noqa: BLE001 — never block startup
        return


def _build_orchestrator(config, store: Store, *, event_sink=None, task=None) -> Orchestrator:
    """Back-compat alias for the shared factory (core/runtime.build_orchestrator).
    Kept as a module-level name because five call sites in this module and
    cli/tui.py resolve it at call time, and tests monkeypatch it here."""
    return build_orchestrator(config, store, event_sink=event_sink, task=task)


async def _run_cli_grill(config, task: Task, store=None) -> Task:
    """B2: Interactive intake grill — one question at a time in the terminal.

    Every round is a billed backend call. The task does not exist yet (it is
    created only after the grill returns, and the operator may Ctrl-C out), so
    there is no attempt row and no task id to bill: each round is booked to the
    ``unattributed_usage`` ledger with ``task_id`` NULL. ``store`` is
    optional so direct callers/tests keep working; without it the spend is
    simply not recorded, exactly as before.
    """
    from ..intake.grill import GrillQuestion, GrillResult, grill_step

    async def _book(step) -> None:
        if store is None:
            return
        try:
            await store.record_unattributed_usage(
                site="cli.task_add.grill",
                model=config.primary_model,
                tokens_used=getattr(step, "tokens_used", 0),
                cache_read_tokens=getattr(step, "cache_read_tokens", 0),
                cache_creation_tokens=getattr(step, "cache_creation_tokens", 0),
            )
        except Exception as exc:  # noqa: BLE001 — accounting never blocks intake
            console.print(f"[dim]intake spend not recorded: {exc}[/]")

    grill_backend = ClaudeBackend(
        model=config.primary_model,
        forbidden_paths=config["safety"]["forbidden_paths"],
        never_push_to=config["git"]["never_push_to"],
        readonly=True,
    )
    qa_history: list[dict] = []
    console.rule("[bold blue]Let's scope this — refining your task spec")
    console.print("[dim]no_human explores the repo and asks clarifying questions.[/]\n")

    while True:
        console.print("[dim]thinking…[/]", end="")
        step = await grill_step(
            task.title, task.description, task.repo_path,
            qa_history, grill_backend,
        )
        console.print("\r", end="")  # clear "thinking…"
        await _book(step)

        if isinstance(step, GrillResult):
            task.title = step.title or task.title
            task.description = step.description or task.description
            if step.acceptance_criteria:
                task.acceptance_criteria = step.acceptance_criteria
            # Human-answered Q&A joins the same audit surface the unattended
            # grill uses (context["intake_qa"] → prompt + PR body), and
            # grill_complete stops the orchestrator's auto-grill from
            # re-asking what the human already answered.
            ctx = task.context or {}
            ctx["intake_qa"] = [
                {"question": qa["question"], "decision_it_changes": "",
                 "answer": qa["answer"], "source": "human", "carve_out": "none"}
                for qa in qa_history
            ]
            ctx["grill_complete"] = True
            task.context = ctx
            console.print()
            console.rule("[bold green]scoping complete")
            console.print(f"  [bold]Title:[/] {task.title}")
            if task.description:
                console.print(f"  [bold]Description:[/] {task.description[:200]}")
            for i, ac in enumerate(task.acceptance_criteria, 1):
                console.print(f"  [green]AC{i}:[/] {ac}")
            console.print()
            return task

        # GrillQuestion — show and get answer
        console.print(f"[bold yellow]Q{step.round}:[/] {step.question}")
        for s in step.suggestions:
            console.print(f"  [cyan]{s}[/]")
        answer = click.prompt("Your answer", default="")
        if not answer.strip():
            answer = "Proceed with what we have"
        qa_history.append({"question": step.question, "answer": answer})


def _persisting(persister, task_id: str, inner):
    """Wrap a console sink so a CLI in-process run also records its events.

    `nh task add --run` and `nh reply --run` drive an Orchestrator directly, so
    they never touched the scheduler's persistence path: nothing reached
    task_events and the board showed "Waiting for events…" forever. Mirrors the
    scheduler's stamping, including leaving a subagent's own task_id alone.
    """
    def sink(event: dict) -> None:
        event.setdefault("ts", time.time())
        event.setdefault("task_id", task_id)
        persister.record(event)
        inner(event)

    return sink


def render_event(event: dict) -> None:
    """Format one orchestrator/agent event as a console line (verbose mode).

    Everything printed here is model- or reviewer-authored prose, so it is
    escaped before it reaches rich: an unescaped "[str]" or "[high]" parses as
    a style tag and rich drops it SILENTLY. That quietly ate the severity grade
    off every review verdict and mangled any evidence mentioning `list[str]`.
    """
    src, kind = event.get("source"), event.get("kind")
    text = escape(event.get("text", "") or "")
    if is_agent_session(src):
        # The coder is the unlabelled default; a planner lens or the aggregator
        # is named, so the console says which role is doing the work.
        who = "" if src == CODER_ROLE else f"[magenta]{src}[/] "
        if kind == "tool_use":
            args = event.get("tool_input") or {}
            summary = escape(", ".join(
                f"{k}={str(v)[:60]}" for k, v in list(args.items())[:3]))
            # emoji=False: tool arguments are file paths, and rich rewrites
            # `:100:` in a path into an emoji. escape() on the tool name too —
            # an MCP server chooses its own names.
            console.print(
                f"  {who}[cyan]→ {escape(str(event.get('tool_name') or ''))}[/]"
                f"([dim]{summary}[/])", emoji=False)
        elif kind == "text" and text.strip():
            console.print(f"  {who}[white]{text.strip()[:500]}[/]")
        elif kind == "thinking" and text.strip():
            console.print(f"  {who}[dim italic]· {text.strip()[:200]}[/]")
        elif kind == "tool_result" and text.strip():
            console.print(f"    [dim]{text.strip()[:200]}[/]")
        elif kind == "result" and not event.get("is_error"):
            # Error results (e.g. max_turns) are reported by the orchestrator's
            # own agent_error/attempt_failed lines; don't double-print them here.
            console.print(
                f"  {who}[dim]· agent done: {event.get('num_turns')} turns, "
                f"{event.get('tokens_used')} tokens[/]"
            )
    else:  # orchestrator
        color = {
            "state": "blue", "commit": "green", "pr_open": "bold green",
            "tests": "yellow", "tamper": "magenta", "escalated": "bold red",
            "failed": "bold red", "stuck": "red", "paused_quota": "yellow",
            "attempt_start": "blue",
        }.get(kind, "dim")
        console.print(f"[{color}]● {kind}[/] {text}")


class CompactProgress:
    """Compact single-line progress for default (non-verbose) mode.

    Shows: [task-id] step | turns=N | elapsed=Xs | last tool: Edit
    Milestones (commit, PR, errors) are printed as persistent lines.
    """
    _MILESTONE_KINDS = {
        "commit", "pr_open", "tests", "escalated", "failed",
        "attempt_start", "attempt_failed", "stuck", "paused_quota",
        "blocker", "profile", "ci_backend",
        # Which model has which role. Printed even in compact mode: it is the
        # one line that makes a silently-shadowed config visible.
        "models",
    }

    def __init__(self, task_id: str):
        self.task_id = task_id[:8]
        self._step = "starting"
        self._turns = 0
        self._last_tool = ""
        self._start = __import__("time").monotonic()
        self._status = console.status(
            self._format(), spinner="dots", spinner_style="blue"
        )
        self._status.start()

    def _format(self) -> str:
        elapsed = int(__import__("time").monotonic() - self._start)
        parts = [
            f"[bold]{self.task_id}[/]",
            f"[blue]{self._step}[/]",
            f"turns={self._turns}",
            f"{elapsed}s",
        ]
        if self._last_tool:
            parts.append(f"[cyan]{self._last_tool}[/]")
        return " · ".join(parts)

    def __call__(self, event: dict) -> None:
        src = event.get("source")
        kind = event.get("kind", "")
        text = event.get("text", "")

        if is_agent_session(src):
            if kind == "tool_use":
                self._last_tool = event.get("tool_name") or ""
            elif kind == "result" and src == CODER_ROLE:
                # Only the implementer's turns count against the turn budget;
                # the planner's own turns are a separate, earlier budget.
                self._turns = event.get("num_turns", self._turns)
        else:
            if "status" in event:
                self._step = event["status"]
            if kind in self._MILESTONE_KINDS:
                # Print milestone as a persistent line, then resume spinner.
                self._status.stop()
                color = {
                    "commit": "green", "pr_open": "bold green",
                    "tests": "yellow", "escalated": "bold red",
                    "failed": "bold red", "attempt_start": "blue",
                    "attempt_failed": "red", "profile": "dim",
                }.get(kind, "dim")
                console.print(f"  [{color}]● {kind}[/] {text}")
                self._status.start()

        self._status.update(self._format())

    def stop(self) -> None:
        self._status.stop()


# --------------------------------------------------------------------------- #


def _launch_shell(repo: str | None = None) -> int:
    """Start the conversational shell. Lazy import: Textual is heavy, and no
    other verb should pay for it."""
    from . import shell as shell_mod

    # Auth belongs to the SERVER, which owns the credential and the runs. The
    # shell is an HTTP client and holds no token, so it must not fail to open
    # on a machine whose profile is mid-setup.
    try:
        config, _ = _bootstrap(require_auth=False)
    except Exception:  # noqa: BLE001 — a broken config must not hide the board
        config = None
    # Read through the module, not a from-import: tests substitute run_shell.
    return shell_mod.run_shell(config=config, repo_path=repo)


def mark_machine_output() -> None:
    """Declare that this command's stdout is machine-readable (JSON), so the
    advisory update notice must stay silent — a notice appended to a JSON body
    corrupts `nh … --json | jq`."""
    try:
        root = click.get_current_context().find_root()
        if isinstance(root.obj, dict):
            root.obj["machine_output"] = True
    except Exception:  # noqa: BLE001 — advisory only
        pass


def _schedule_update_notice(ctx: click.Context) -> None:
    """Arrange for a one-line "newer version available" notice after the command.

    Wrapped end to end: an update check is the last thing that may ever break a
    real command, so every failure mode here degrades to silence.
    """
    try:
        from ..updates import check_for_update

        # create_if_missing=False: an update check must not have the side
        # effect of writing a config file for someone who never made one.
        try:
            config = load_config(create_if_missing=False)
        except Exception:  # noqa: BLE001 - no config just means default settings
            config = None
        notice = check_for_update(__version__, config=config)
        if not notice:
            return

        def _print() -> None:
            try:
                # Piped/redirected stdout: a log consumer or `| jq`, never the
                # right audience for an advisory line, and printing it on
                # stderr there still risks a machine-output command that
                # writes JSON to stdout only (checked next) reading as
                # corrupted by a caller that merges streams.
                if not sys.stdout.isatty():
                    return
                # The command emitted (or was about to emit) JSON on stdout —
                # `--json`/`--json-out` — so even on an interactive TTY the
                # notice must not appear, since callers pipe interactively too.
                if isinstance(ctx.obj, dict) and ctx.obj.get("machine_output"):
                    return
                click.echo(notice, err=True)
            except Exception:  # noqa: BLE001
                pass

        ctx.call_on_close(_print)
    except Exception:  # noqa: BLE001 - never let this reach the operator
        pass


@click.group(
    invoke_without_command=True,
    # The installed app ships NO documentation — 0 .md files in the bundle,
    # verified by mounting the round-3 DMG. The only documents in
    # Contents/Resources are LICENSE, LICENSE.electron.txt and
    # LICENSES.chromium.html, which are notices a redistribution owes, not
    # something a user reads to learn the product. So `--help` was the whole
    # manual, and it named no next step.
    #
    # It points at the SITE, deliberately, and not at the GitHub repo: the
    # repository is private until the operator makes it public, and a link that
    # 404s for every user is worse than no link. Revisit once it is public.
    # CANONICAL /docs, not /docs.html — the latter only reaches the page
    # through a 307, and the site's own markup links /docs in all five
    # places. A redirect is a thing someone eventually retires.
    epilog="Docs: https://getnohuman.com/docs",
)
@click.version_option(__version__, prog_name="nh")
@click.option("--repo", default=None, type=click.Path(),
              help="Repo the shell files tasks against (default: the git repo you are in).")
@click.pass_context
def cli(ctx: click.Context, repo: str | None) -> None:
    """no_human — autonomous AI software delivery (runs on your own Claude credentials).

    Run `nh` with no arguments for the conversational shell: the lanes, the
    event tail, and an intake you talk to in plain English. Every verb below
    still works exactly as it did.
    """
    # `--repo` exists at both levels, so `nh --repo X shell` and
    # `nh shell --repo X` both read naturally — and the group-level one used to
    # be silently dropped the moment a subcommand followed it. Park it where
    # the subcommand can find it.
    ctx.obj = {"repo": repo, "machine_output": False}
    # An update notice, printed AFTER the command's own output so it never
    # displaces what the operator ran nh for. `--version` is a click eager
    # option and has already exited by this point, so the fastest path stays
    # untouched. Nothing here touches the network on this thread: the notice is
    # rendered from a cache an earlier invocation wrote (see updates.py).
    if ctx.invoked_subcommand is not None:
        _schedule_update_notice(ctx)
    if ctx.invoked_subcommand is None:
        # The Textual shell takes over the terminal on an alternate screen, so
        # a notice printed around it would be wiped before it could be read.
        ctx.exit(_launch_shell(repo))


@cli.command("shell")
@click.option("--repo", default=None, type=click.Path(),
              help="Repo the shell files tasks against (default: the git repo you are in).")
@click.pass_context
def shell_cmd(ctx: click.Context, repo: str | None) -> None:
    """The conversational shell — the same thing bare `nh` opens.

    Talks to the running server over HTTP (start it with `nh start`), shows
    the board's lanes, and takes plain English through the same scoping questions
    the web composer uses.
    """
    sys.exit(_launch_shell(repo or (ctx.obj or {}).get("repo")))


@cli.command("init")
@click.option("--non-interactive", is_flag=True,
              help="Ask nothing. For provisioning scripts, Dockerfiles and CI.")
@click.option("--auth-mode", type=click.Choice(["subscription", "api_key"]),
              default=None,
              help="Which credential pays (--non-interactive only; default: "
                   "subscription). The wizard asks this question instead.")
@click.option("--token-stdin", is_flag=True,
              help="Read the credential as one line from stdin "
                   "(--non-interactive only). There is deliberately no --token "
                   "argument: argv is world-readable in `ps` and lands in shell "
                   "history.")
@click.option("--repo", "repo_arg", default=None, type=click.Path(),
              help="Onboard this repo (--non-interactive only).")
@click.option("--no-repo", is_flag=True,
              help="Onboard no repo (--non-interactive only).")
def init_cmd(non_interactive, auth_mode, token_stdin, repo_arg, no_repo):
    """Set up no_human from scratch: prerequisites, token, config, first repo.

    Safe to run again — never overwrites existing config, secrets, or data.

    \b
    Scripted (KI-2): asks nothing, writes exactly what the wizard writes.
      printf %s "$TOKEN" | nh init --non-interactive --token-stdin --no-repo
      nh init --non-interactive --auth-mode api_key --token-stdin --repo ~/git/x
    """
    from .init_cmd import (
        check_prerequisites,
        ensure_config,
        ensure_home_dir,
        offer_onboard,
        print_summary,
        resolve_repo_arg,
        setup_token,
        setup_token_noninteractive,
    )

    # The scripted flags are meaningless without the mode they belong to, and
    # silently ignoring `--token-stdin` would leave a script blocked on a pipe
    # nobody reads while the wizard prompts for the token it was handed.
    if not non_interactive:
        stray = [name for name, given in (
            ("--auth-mode", auth_mode is not None),
            ("--token-stdin", token_stdin), ("--repo", repo_arg is not None),
            ("--no-repo", no_repo),
        ) if given]
        if stray:
            raise click.UsageError(
                f"{', '.join(stray)} require --non-interactive.")
    elif repo_arg is not None and no_repo:
        raise click.UsageError("--repo and --no-repo contradict each other.")

    console.rule("[bold]no_human — first-time setup")

    # 1. Prerequisites.
    console.print("\n[bold]1. Checking prerequisites[/]")
    errors, warnings = check_prerequisites()
    if errors:
        console.print(f"\n[red]Missing {len(errors)} required tool(s). "
                       "Install them and re-run `nh init`.[/]")
        sys.exit(1)

    # 2. Home directory.
    console.print("\n[bold]2. Home directory[/]")
    created = ensure_home_dir()
    if created:
        console.print("  [green]✓[/] created ~/.no_human/ (mode 700)")
    else:
        console.print("  [green]✓[/] ~/.no_human/ exists")

    # 3. Billing / authentication.
    console.print("\n[bold]3. Authentication[/]")
    if non_interactive:
        auth_mode = auth_mode or "subscription"
        # One line, from stdin, never from argv. A `--token <value>` would put
        # the credential in `ps` output, in the shell's history file, and in
        # any CI log that echoes the command it ran.
        token = sys.stdin.readline().strip() if token_stdin else None
        token_ready = setup_token_noninteractive(auth_mode, token)
        if not token_ready:
            # The wizard prints a summary card saying "Token not set" and exits
            # 0, because a human is reading it. A script's only channel is the
            # exit code, and an install that cannot make a call must not report
            # success. Nothing was stored; config.yaml is left as it was.
            raise click.ClickException(
                "the credential was refused (see above). Fix it and re-run — "
                "no credential was stored.")
    else:
        token_ready, auth_mode = setup_token()

    # 4. Config file.
    console.print("\n[bold]4. Configuration[/]")
    ensure_config(auth_mode=auth_mode)

    # 5. Optional: onboard a repo.
    console.print("\n[bold]5. Repo onboarding[/]")
    if non_interactive:
        repo_path = resolve_repo_arg(repo_arg) if repo_arg else None
        if repo_arg and not repo_path:
            # The wizard carries on without a repo because a human can see the
            # error and re-run one command. A script cannot, and a half-set-up
            # install that reports success is the failure this mode exists to
            # remove.
            raise click.ClickException(
                f"--repo {repo_arg} cannot be onboarded (see above). "
                f"Authentication and config were already written.")
        if repo_path is None:
            console.print("  [dim]skipped (no --repo)[/]")
    else:
        repo_path = offer_onboard()
    if repo_path:
        # Run onboard inline — reuse the existing nh onboard logic.
        # Catch SystemExit so a failing onboard doesn't kill init.
        # soft_wrap on every line carrying a copy-able command + path: Rich
        # folds a long path mid-token at the ambient console width.
        console.print(f"  Running [bold]nh onboard {repo_path}[/] …", soft_wrap=True)
        try:
            ctx = click.Context(onboard, info_name="nh onboard")
            ctx.invoke(onboard, repo=repo_path, confirm=False, agent=False)
        except SystemExit:
            console.print(
                "  [yellow]onboarding did not complete — you can re-run:[/]\n"
                f"    [bold]nh onboard {repo_path}[/]",
                soft_wrap=True,
            )
        else:
            # Auto-confirm if the test command was proven — don't force a
            # separate `--confirm` step for an already-verified profile.
            try:
                ctx2 = click.Context(onboard, info_name="nh onboard")
                ctx2.invoke(onboard, repo=repo_path, confirm=True, agent=False)
            except SystemExit:
                console.print(
                    f"\n  To confirm the profile:\n"
                    f"    [bold]nh onboard {repo_path} --confirm[/]",
                    soft_wrap=True,
                )

    # 6. Summary.
    console.print()
    from ..config import CONFIG_PATH
    print_summary(
        token_ready=token_ready,
        config_path=CONFIG_PATH,
        repo_path=repo_path,
    )


_KNOWN_TIERS = ("trivial", "simple", "standard", "complex")


def format_tier_summary(
    tier: str,
    signals: list,
    *,
    predicted: bool,
    moa_min_signals: int = 2,
    moa_enabled: bool = False,
    moa_proposers: int = 3,
) -> str:
    """Compact human-readable resourcing summary for a task's complexity tier.

    Pure and Store/LLM-free — every rule here mirrors a live gate so the
    diagnostic can't silently drift from what actually ran:
      - MoA planning fan-out: gated FIRST by ``llm.moa_planning.enabled``
        (orchestrator.py's `_generate_plan`, `if moa_cfg.get("enabled", False):`
        — a global kill switch that short-circuits everything else), and only
        then by tier/signal count (`tier == "complex" or len(signals) >=
        min_signals`). Reporting "applied" while ``enabled=False`` would be a
        lie about the one gate an operator can turn off outright.
      - Extended thinking: ``core.complexity.is_complex`` (`tier == "complex"`).
      - Complex-tier angle review passes: `Reviewer._tier_wants_angles`
        (`tier == "complex"`), naming the two angles (security, tests).
    """
    label = "predicted (task has not run)" if predicted else "recorded"
    fired = ", ".join(signals) if signals else "none"
    tier_display = tier if tier in _KNOWN_TIERS else f"{tier} (unrecognized tier)"

    is_complex_tier = tier == "complex"
    gate_would_fire = is_complex_tier or len(signals) >= moa_min_signals
    if not moa_enabled:
        moa_line = ("MoA planning fan-out: not applied "
                    "(disabled globally: llm.moa_planning.enabled=False)")
        moa_mark = "·"
    elif gate_would_fire:
        moa_line = f"MoA planning fan-out: applied ({moa_proposers} Opus proposers)"
        moa_mark = "✓"
    else:
        moa_line = f"MoA planning fan-out: not applied (signals {len(signals)}/{moa_min_signals})"
        moa_mark = "·"

    if is_complex_tier:
        thinking_mark, thinking_line = "✓", "extended thinking: on"
        angles_mark = "✓"
        angles_line = "complex-tier angle review passes: applied (security, tests)"
    else:
        thinking_mark, thinking_line = "·", "extended thinking: off"
        angles_mark = "·"
        angles_line = "complex-tier angle review passes: not applied"

    lines = [
        f"tier: {tier_display} ({label})",
        f"signals: {fired}",
        "resourcing:",
        f"  {moa_mark} {moa_line}",
        f"  {thinking_mark} {thinking_line}",
        f"  {angles_mark} {angles_line}",
    ]
    if tier == "trivial":
        # The fast path must be READABLE, not inferable from a shorter runtime.
        # The file names below are a hand-kept summary of the predicate's own
        # constants — see the drift note on `TRIVIAL_FAST_PATH_NOTE`.
        # Mirrors orchestrator.TRIVIAL_FAST_PATH_NOTE and the events each stage
        # emits — same rule as every other line here: one live gate per line.
        lines += [
            "trivial-tier fast path (ceremony reduced, gates unchanged):",
            "  − intake scoping questions: skipped",
            "  − planner: utility model, ≤2 turns, no MoA fan-out",
            "  − skill discovery: skipped",
            "  − review: bounded single pass (fresh context, cited pass/fail)",
            "  ✓ unchanged: review gate, tamper guard, export gate, human merge",
            "  ↑ escalates to full ceremony if the plan or diff leaves "
            "≤2 prose files (deletions included), or if the diff edits agent "
            "instructions (.agents/, CLAUDE.md, AGENTS.md) or gate control "
            "data (EXPORT_CLASSIFICATION.txt, RELEASE_MANIFEST.txt) — those "
            "keep the full review",
        ]
    return "\n".join(lines)


@cli.group()
def task() -> None:
    """Manage tasks."""


@task.command("add")
@click.argument("source", required=False)
@click.option("--title", default=None, help="Freeform task title (instead of a URL).")
@click.option("--repo", required=True, type=click.Path(exists=True), help="Target repo path.")
@click.option("--description", default=None, help="Longer description.")
@click.option("--criteria", multiple=True, help="Acceptance criterion (repeatable).")
@click.option("--external-id", default=None, help="External id, e.g. PROJ-123.")
@click.option("--kind", default=None,
              help="Override the task type (feature|bugfix|ci_fix|traceability|test_gap).")
@click.option("--linked-repo", multiple=True, type=click.Path(exists=True),
              help="Additional repo path for multi-repo tasks (repeatable).")
@click.option("--run/--no-run", default=True, help="Run immediately (default) or just stage.")
@click.option("-v", "--verbose", is_flag=True, help="Show full tool-call log (default: compact progress).")
@click.option("--grill/--no-grill", default=True,
              help="Ask a few questions to refine the spec (default: on; --no-grill to skip).")
@click.option("--backend", default=None, type=click.Choice(list(SUPPORTED_BACKENDS)),
              help="Run THIS task's coder on the named backend instead of "
                   "`worker.backend` from config. Only the coder moves; planner, supervisor "
                   "and utility stay on Claude, and the reviewer unless overridden in Settings.")
@click.option("--approve-plan", is_flag=True, default=False,
              help="Stop after planning and wait for you to approve the plan "
                   "before any implementation token is spent.")
@click.option("--priority", default=None, type=click.Choice(list(PRIORITY_ORDER)),
              help="Dispatch priority, high|medium|low (default: medium). Orders "
                   "the PENDING queue only, with no aging: a low task can wait "
                   "behind an unbounded medium/high stream, and is never preempted "
                   "once a task starts running.")
def task_add(source, title, repo, description, criteria, external_id, kind, linked_repo, run, verbose, grill, backend, approve_plan, priority):
    """Add a task — from a GitHub/GitLab issue URL, a plain sentence, or --title.

    A positional SOURCE is either an issue URL or a plain sentence: an issue
    URL (one containing /issues/ or /-/issues/) is ingested from its source;
    a plain sentence is filed directly, using it as the task title, same as
    --title. A source-shaped token that is not an issue URL — a bare ticket
    key (PROJ-42) or `owner/repo#12` — is still refused with "not a
    recognized task URL/id": the standalone tracker adapter that once
    accepted bare keys was removed; Jira issues arrive through the poller
    instead (`integrations.jira` in config.yaml — see docs/adapters.md#jira).

    Examples:
      nh task add https://code.example.com/org/repo/issues/12 --repo ~/repo
      nh task add "Fix the flaky E2E test" --repo ~/repo
      nh task add --title "Fix X" --repo ~/repo --criteria "..."
    """
    config, _ = _bootstrap()

    # The grill asks one question at a time at a click.prompt. With no terminal
    # there is nobody to answer it, and `nh task add … | tee` died on
    # `Your answer []: Aborted!` (walkthrough B9) — even under --no-run. Bare
    # `nh` already refuses the same way rather than hanging; this reuses its
    # check instead of keeping a second opinion about what interactive means.
    if grill and not stdio_is_interactive():
        console.print("[dim]no terminal on stdin/stdout — skipping the scoping "
                      "questions (same as --no-grill).[/]")
        grill = False

    async def _go():
        async with Store(config.db_path) as store:
            if source and is_plain_text_task(source):
                if title:
                    console.print(
                        "[red]pass either a plain-text description or --title, "
                        "not both[/]"
                    )
                    sys.exit(1)
                t = Task.new(source.strip(), repo_path=str(Path(repo).resolve()),
                             description=description, external_id=external_id)
                t.acceptance_criteria = list(criteria)
                if grill:
                    t = await _run_cli_grill(config, t, store)
            elif source:
                ref = parse_source(source)
                console.print(f"[blue]ingesting[/] {ref.kind}: {ref.ref}")
                try:
                    t = ingest_from_url(source, config.data)
                except Exception as exc:  # noqa: BLE001
                    console.print(f"[red]intake failed:[/] {exc}")
                    sys.exit(1)
                t.repo_path = str(Path(repo).resolve())
                t.acceptance_criteria += list(criteria)
                if grill:
                    t = await _run_cli_grill(config, t, store)
            elif title:
                t = Task.new(title, repo_path=str(Path(repo).resolve()),
                             description=description, external_id=external_id)
                t.acceptance_criteria = list(criteria)
                if grill:
                    t = await _run_cli_grill(config, t, store)
            else:
                console.print("[red]provide a SOURCE url/id or --title[/]")
                sys.exit(1)
            # WS-E: attach linked repos for multi-repo tasks.
            if linked_repo:
                t.linked_repos = [str(Path(r).resolve()) for r in linked_repo]
                # D19: fail at intake, not silently mid-attempt. A linked repo the
                # agent cannot stage is a repo it can never commit to — and the
                # planner will have written a plan that names its files.
                from ..core.multi_repo import validate_linked_repos
                errors = validate_linked_repos(t)
                if errors:
                    # validate_linked_repos checks the primary repo too, so the
                    # label stays generic — the message names the offending path.
                    for err in errors:
                        print_path_error(console, "[red]multi-repo intake:[/]", err)
                    sys.exit(1)
            # WS-A: tag the task with its type so the right pipeline drives it.
            verdict = classify_kind(t, override=kind)
            t.kind = verdict.kind.value
            # Defect 204f2177: a report-only kind (design_doc/investigation)
            # paired with test-bearing criteria (a CLI flag, red-first tests,
            # a shipped artifact) can never be satisfied by that kind's
            # report-only completion — refuse at intake rather than silently
            # applying the mismatched kind. Never auto-accept; the human must
            # pick an implement-shaped --kind or drop the test-bearing ask.
            mismatch = kind_criteria_mismatch(t.kind, t.acceptance_criteria)
            if mismatch:
                console.print(f"[red]intake refused:[/] {mismatch}")
                console.print(
                    "[dim]pass an explicit --kind (e.g. feature/bugfix) that "
                    "ships the demanded artifact, or drop the test-bearing "
                    "criteria if this really is a report-only deliverable.[/]"
                )
                sys.exit(1)
            if backend:
                t.config["backend"] = backend
            if priority:
                t.priority = normalise_priority(priority)
            if approve_plan:
                from ..core.plan_gate import CONFIG_KEY as _PLAN_APPROVAL_KEY
                t.config[_PLAN_APPROVAL_KEY] = True
            # SCRUM-48: repo default budgets apply here too, not just web create —
            # an explicit key already on t.config (e.g. set by grill/intake) wins.
            from ..profile import ProjectProfile, apply_default_task_config
            prof = await store.get_profile(t.repo_path) or ProjectProfile.load(t.repo_path)
            t.config = apply_default_task_config(prof, t.config)
            await store.create_task(t)
            console.print(f"[green]created task[/] [bold]{t.id[:8]}[/] — {t.title}")
            console.print(f"  [magenta]kind:[/] {t.kind}  [dim]({verdict.reason})[/]")
            if backend:
                console.print(f"  [cyan]backend:[/] {backend}")
            if t.linked_repos:
                console.print(f"  [cyan]multi-repo:[/] {len(t.linked_repos) + 1} repos")
            if t.acceptance_criteria:
                console.print(f"  {len(t.acceptance_criteria)} acceptance criteria")
            # Warn only if the profile won't actually drive the task under the
            # active policy — a proven profile with profile.auto_confirm_proven
            # on IS usable even without a human confirm click, so warning then
            # would be a lie (it drove ca23ce68 to a clean PR while this warned).
            auto = bool(config.data.get("profile", {}).get("auto_confirm_proven", False))
            if not prof or not prof.usable_under_policy(auto_confirm_proven=auto):
                # soft_wrap for the same reason print_path_error has it: these
                # two lines are commands the user copies, and Rich's default
                # wrap folds `nh onboard <long path>` mid-command at whatever
                # width the ambient environment happens to imply — 80 whenever
                # stdout is not a terminal. A folded command is not a command.
                console.print(
                    "[yellow]⚠ repo profile not usable[/] — test command will be "
                    "auto-detected (may be wrong). Run both:\n"
                    f"  [bold]nh onboard {t.repo_path}[/]"
                    "            [dim]# derive + prove[/]\n"
                    f"  [bold]nh onboard {t.repo_path} --confirm[/]"
                    "  [dim]# then confirm[/]",
                    soft_wrap=True,
                )
            if not run:
                console.print(f"staged. run it with:  [bold]nh watch {t.id[:8]}[/]")
                return
            if _server_owns_worker(config):
                # A new task is PENDING, which the server's scheduler claims.
                console.print(
                    "[cyan]the running server picked it up[/] — "
                    f"watch it with: [bold]nh watch {t.id[:8]}[/]"
                )
                return
            if verbose:
                sink = render_event
            else:
                sink = CompactProgress(t.id)
            if verbose:
                console.rule(f"[bold]running {t.id[:8]}")
            async with EventPersister(store, t.id) as persister:
                orch = _build_orchestrator(
                    config, store, event_sink=_persisting(persister, t.id, sink), task=t)
                outcome = await orch.run_task(t)
            if not verbose:
                sink.stop()
            console.rule(f"[bold]{outcome.status.value}")
            if outcome.pr_url:
                console.print(f"[bold green]PR:[/] {outcome.pr_url}")
            console.print(outcome.detail)

    asyncio.run(_go())


@task.command("context")
@click.argument("task_id")
def task_context(task_id):
    """Gather and show context for a staged task (no implementation run)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                return
            gatherer = ContextGatherer(build_default_sources(store, config.data))
            ctx = await gatherer.gather(t)
            t.context = {**(t.context or {}), "gathered": ctx.to_dict()}
            await store.update_task(t)
            for c in ctx.chunks:
                console.print(f"[cyan]\\[{c.source}][/] {c.title}  [dim]{c.ref}[/]")
            if ctx.errors:
                for src, err in ctx.errors.items():
                    console.print(f"[yellow]! {src}: {err}[/]")
            comp = ctx.completeness
            verdict = "[green]complete[/]" if comp and comp.ok else "[yellow]incomplete[/]"
            console.print(f"\ncompleteness: {verdict}")
            if comp:
                console.print(f"  present: {comp.present}")
                if comp.missing:
                    console.print(f"  [yellow]missing: {comp.missing}[/]")

    asyncio.run(_go())


@task.command("tier")
@click.argument("task_id")
def task_tier(task_id):
    """Show a task's complexity tier and the resourcing it bought (read-only)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            ctx = t.context or {}
            # `or {}`: config.yaml is hand-edited and a bare `llm:`/`moa_planning:`
            # with its body commented out deep-merges to None, not to a dict.
            moa_cfg = (config.get("llm") or {}).get("moa_planning") or {}
            if ctx.get("complexity_tier"):
                tier = ctx["complexity_tier"]
                signals = list(ctx.get("complexity_signals") or [])
                predicted = False
            else:
                from ..core.complexity import compute_tier
                tier, signals = compute_tier(t, moa_cfg)
                predicted = True
            console.print(f"[bold]{t.id}[/]  {t.title}")
            console.print(format_tier_summary(
                tier, signals,
                predicted=predicted,
                moa_min_signals=int(moa_cfg.get("min_signals", 2)),
                moa_enabled=bool(moa_cfg.get("enabled", False)),
                moa_proposers=int(moa_cfg.get("proposers", 3)),
            ))

    asyncio.run(_go())


@task.command("config")
@click.argument("task_id")
@click.argument("assignments", nargs=-1, required=True)
def task_config(task_id, assignments):
    """Set human-only per-task overrides: nh task config TASK_ID KEY=VALUE ...

    Human-only by construction — this CLI is the operator's tool, the agent
    never calls it. Accepts the keys the orchestrator reads as per-task
    overrides (size limits, lifetime caps, the per-attempt token cap) plus
    `priority` (high|medium|low), which sets the task's dispatch priority
    column directly rather than going through `task.config`. The human is
    the gate: this sets the exact requested value, raising or lowering an
    existing cap. (Blocker options still never lower — see `apply_action`.)
    """
    from ..blockers import ActionError, apply_action, human_event
    from ..core.bounds import Bounds

    config, _ = _bootstrap(require_auth=False)

    settings: dict[str, str] = {}
    for assignment in assignments:
        if "=" not in assignment:
            console.print(f"[red]malformed assignment (want KEY=VALUE):[/] {assignment}")
            sys.exit(1)
        key, _, value = assignment.partition("=")
        settings[key.strip()] = value.strip()

    new_priority = settings.pop("priority", None)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)

            priority_note = None
            if new_priority is not None:
                # `normalise_priority("")` reads as "unset -> default", the
                # right call for a DB row with a blank column; a human
                # writing `priority=` here almost certainly typo'd or
                # emptied an assignment, not asked for medium — reject it
                # rather than silently applying a value they never named.
                if not new_priority:
                    console.print(
                        "[red]priority requires a value: one of "
                        f"{', '.join(PRIORITY_ORDER)}[/]")
                    sys.exit(1)
                try:
                    priority_note = normalise_priority(new_priority)
                except ValueError as exc:
                    console.print(f"[red]{exc}[/]")
                    sys.exit(1)

            applied = ""
            if settings:
                try:
                    applied = apply_action(
                        t, {"set_task_config": settings}, human_override=True,
                        bounds=Bounds.from_config(config.get("bounds")))
                except ActionError as exc:
                    console.print(f"[red]{exc}[/]")
                    sys.exit(1)

            prior_priority = t.priority
            if priority_note is not None:
                t.priority = priority_note
                applied = ", ".join(
                    p for p in (applied, f"priority={priority_note}") if p)

            # Write the column BEFORE the event that attests to it: if the
            # column write fails, no event claims a change that didn't
            # happen (the evidence-gap class `nh doctor` is built to catch).
            await store.update_task_columns(t)

            if priority_note is not None:
                await store.save_events(t.id, [{
                    **human_event(
                        "priority", prior_status=t.status,
                        reason=f"{prior_priority} -> {priority_note}",
                        text=f"priority set to {priority_note} by human"),
                    "ts": time.time(),
                }])

            console.print(f"[green]applied[/] {applied}")

    asyncio.run(_go())


REPO_CONFIG_KEYS = frozenset({"default_attempt_tokens", "default_lifetime_tokens"})


@cli.group("repo")
def repo_group() -> None:
    """Manage per-repo profile settings."""


@repo_group.command("config")
@click.argument("repo_path", type=click.Path(exists=True))
@click.argument("assignments", nargs=-1)
def repo_config(repo_path, assignments):
    """Set or inspect human-only repo profile defaults (SCRUM-26).

    nh repo config REPO_PATH                 — inspect current defaults
    nh repo config REPO_PATH KEY=VALUE ...   — set defaults

    Human-only by construction, like `nh task config` — this is the
    operator's calibration knob for a repo's default per-task token budgets.
    They are copied into task.config at task creation whenever the task has
    no explicit override; an explicit `nh task config` value always wins.
    Accepts exactly default_attempt_tokens / default_lifetime_tokens.

    UNIT — COST-WEIGHTED tokens, the same unit as the caps themselves and the
    same unit `nh task config` takes. Since 2026-07-31 `bounds.attempt_tokens`
    / `bounds.lifetime_tokens` are cost-weighted (fresh in/out x1.0, cache
    write x1.25, cache read x0.1 — core.pricing) and default to 2,000,000 /
    4,000,000 (raised 2026-08-03; rationale on core.bounds.Bounds). Every write
    here stamps `default_budget_unit: weighted` on the profile, which stamps
    `budget_unit` into the task config, so the value is read at face value and
    never converted.

    CHANGED 2026-08-10, and it changed because of what it cost (R1, funnel
    forensics). This field USED to take raw tokens and be converted x0.1985 on
    read, with this docstring telling operators the weighted defaults
    "correspond to roughly 10,100,000 / 20,200,000 in this field". The
    `no_human` profile held 12,000,000 — a number that looks like a generous
    raise and read as 2,382,000, 40% BELOW the ungranted default. 32 of 33
    August tasks ran under that cut and none of them merged. Asking an operator
    to type one unit while the product enforces another was the defect; there
    is now one unit.

    Values already stored WITHOUT the stamp are still read as raw and
    converted, because they were written under the old contract. Re-set them
    here to move them over — `nh repo config REPO` prints which unit each is
    in. Note that a pre-cutover value must be RE-DERIVED, not re-typed: a habit
    of typing 20,200,000 now means 20,200,000 weighted, 5x the default, so the
    write echoes the ratio to the ungranted default.

    Moving a pre-cutover profile over therefore takes ONE command carrying
    EVERY budget key. A partial write on an unstamped profile is REFUSED:
    `default_budget_unit` is a single field describing both values, so
    stamping it on a one-key write would silently re-declare the untouched
    sibling as weighted — about 5x, permanently, with no floor and no warning
    because a stamped value is taken at face value.
    """
    from ..core.bounds import Bounds
    from ..core.pricing import WEIGHTED_UNIT
    from ..profile import ProjectProfile

    config, _ = _bootstrap(require_auth=False)
    repo = str(Path(repo_path).expanduser().resolve())

    resolved: dict[str, int] = {}
    for assignment in assignments:
        if "=" not in assignment:
            console.print(f"[red]malformed assignment (want KEY=VALUE):[/] {assignment}")
            sys.exit(1)
        key, _, raw = assignment.partition("=")
        key = key.strip()
        if key not in REPO_CONFIG_KEYS:
            console.print(
                f"[red]{key!r} is not settable on a repo profile "
                f"(allowed: {', '.join(sorted(REPO_CONFIG_KEYS))})[/]"
            )
            sys.exit(1)
        try:
            value = int(raw.strip())
        except (TypeError, ValueError):
            console.print(f"[red]{key} must be an integer, got {raw!r}[/]")
            sys.exit(1)
        if value <= 0:
            console.print(f"[red]{key} must be positive, got {value}[/]")
            sys.exit(1)
        resolved[key] = value

    async def _go():
        async with Store(config.db_path) as store:
            profile = await store.get_profile(repo) or ProjectProfile(repo_path=repo)
            if not resolved:
                console.print(f"default_attempt_tokens={profile.default_attempt_tokens or 0}")
                console.print(f"default_lifetime_tokens={profile.default_lifetime_tokens or 0}")
                # R1: the unit these two are in used to be knowable only by
                # doing the 0.1985 arithmetic by hand against a docstring. It
                # is the thing that made the August cut invisible, so it is
                # printed next to the numbers whose meaning it decides.
                if profile.default_budget_unit == WEIGHTED_UNIT:
                    console.print(f"budget_unit={WEIGHTED_UNIT}")
                else:
                    console.print(
                        "budget_unit=raw [dim](written before the 2026-07-31"
                        " cutover; converted x0.1985 on read — re-set it here"
                        " to store it in the weighted unit)[/]"
                    )
                return
            # THE MARKER DESCRIBES THE WHOLE PROFILE. `default_budget_unit` is
            # one field for both values, so a PARTIAL write cannot honestly
            # claim it: stamping on a one-key write re-declares the untouched,
            # still-pre-cutover sibling as weighted, and a stamped value is
            # taken at face value — no conversion, no floor, no warning. On
            # the live profile ({10,100,000 / 20,200,000} unstamped) a write
            # of default_lifetime_tokens alone moved the ENFORCED attempt cap
            # from 2,004,850 to 10,100,000: 5.0x, permanent, fail-open.
            #
            # So refuse, rather than guess. The human is right here and can
            # type both numbers; converting the sibling for them would change
            # a value they did not touch, which is how this class of bug got
            # started. (Third instance of it on this branch — task.config's
            # dict-wide marker, then the profile->task copy, now the write
            # that creates the stamp. The rule: a marker over a record may be
            # written only when every value in that record is in the unit it
            # claims.)
            stale = sorted(
                k for k in REPO_CONFIG_KEYS - set(resolved) if getattr(profile, k, 0))
            if stale and profile.default_budget_unit != WEIGHTED_UNIT:
                names = ", ".join(f"{k}={getattr(profile, k):,}" for k in stale)
                console.print(
                    f"[red]refusing a partial write:[/] this profile is still "
                    f"in the pre-cutover RAW unit and carries {names}, which "
                    f"this command does not set. Recording "
                    f"{', '.join(sorted(resolved))} alone would declare the "
                    f"whole profile cost-weighted and silently re-type "
                    f"{' and '.join(stale)} as weighted — roughly 5x, "
                    f"permanently.\n"
                    f"  Either write every budget key in ONE command:\n"
                    f"    nh repo config {repo_path} "
                    + " ".join(
                        f"{k}=<weighted>" for k in sorted(REPO_CONFIG_KEYS))
                    + f"\n  or re-type just the sibling in COST-WEIGHTED "
                    f"tokens first ({' and '.join(stale)}), then repeat this "
                    f"command. `nh repo config {repo_path}` prints the current "
                    f"values and which unit they are in."
                )
                sys.exit(1)
            for key, value in resolved.items():
                setattr(profile, key, value)
            # Every write is in the current unit, and says so. This is what
            # kills the ambiguity class rather than flooring it forever.
            profile.default_budget_unit = WEIGHTED_UNIT
            await store.upsert_profile(profile)
            console.print(
                "[green]applied[/] " + ", ".join(f"{k}={resolved[k]}" for k in sorted(resolved))
                + " [dim](cost-weighted tokens)[/]"
            )
            # The ratio to the ungranted default, at the one moment a 5x typo
            # is cheap to catch: a pre-cutover habit types 20,200,000 here.
            bounds = Bounds.from_config((config.get("bounds") or {}))
            for key in sorted(resolved):
                default = getattr(bounds, key.removeprefix("default_"))
                console.print(
                    f"  [dim]{key}: {resolved[key]:,} vs the ungranted default "
                    f"{default:,} — {resolved[key] / default:.1f}x[/]"
                )

    asyncio.run(_go())


@task.command("list")
def task_list():
    """List all tasks as a board."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            tasks = await store.list_tasks()
            table = Table(title="no_human tasks")
            table.add_column("id", style="bold")
            table.add_column("kind", style="magenta")
            table.add_column("status")
            table.add_column("att", justify="right", style="dim")
            table.add_column("turns", justify="right", style="dim")
            table.add_column("title")
            table.add_column("repo", style="cyan")
            table.add_column("PR", style="green")
            for t in tasks:
                attempts = await store.list_attempts(t.id)
                att_n = str(len(attempts)) if attempts else "—"
                last_turns = "—"
                pr_url = ""
                for a in reversed(attempts):
                    if a.get("turns_used") and last_turns == "—":
                        last_turns = str(a["turns_used"])
                    if a.get("pr_url") and not pr_url:
                        pr_url = a["pr_url"]
                repo_name = t.repo_path.rstrip("/").rsplit("/", 1)[-1] if t.repo_path else ""
                status_str = t.status.value
                status_colors = {
                    "done": "green", "failed": "red", "escalated": "bold red",
                    "awaiting_approval": "yellow", "implementing": "blue",
                }
                color = status_colors.get(status_str, "")
                styled_status = f"[{color}]{status_str}[/]" if color else status_str
                table.add_row(
                    t.id[:8], t.kind, styled_status, att_n, last_turns,
                    t.title[:50], repo_name[:20],
                    "✓" if pr_url else "",
                )
            console.print(table)

    asyncio.run(_go())


@task.command("show")
@click.argument("task_id")
def task_show(task_id):
    """Show a task's requirements, attempts, and evidence."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                return
            console.print(f"[bold]{t.id}[/]  [blue]{t.status.value}[/]  [magenta]{t.kind}[/]")
            events = await store.list_events(t.id)
            if is_waiting_for_slot(events, status=t.status.value):
                waits = [e for e in events if e.get("kind") == slot_wait.KIND]
                stats = _running_pool_stats(config)
                pause = stats[2] if stats else None
                if pause:
                    console.print(f"[magenta]{slot_wait.pool_paused_text(pause)}[/]")
                elif stats is None:
                    console.print(
                        f"[blue]{waits[-1]['text']}[/] "
                        f"[dim]({slot_wait.STALE_POOL_NOTE})[/]")
                else:
                    console.print(f"[blue]{waits[-1]['text']}[/]")
            console.print(f"title: {t.title}")
            if t.description:
                console.print(f"description: {t.description}")
            if t.acceptance_criteria:
                console.print("acceptance criteria:")
                for c in t.acceptance_criteria:
                    console.print(f"  - {c}")
            console.print(f"repo: {t.repo_path}")
            if t.blocker:
                console.print(f"[red]blocker:[/] {t.blocker}")
            lat = (t.blocker or {}).get("escalation_latency") if t.blocker else None
            if lat and t.status is TaskStatus.ESCALATED:
                console.print(
                    f"[yellow]Escalated at attempt {lat['attempts_before_escalation']} "
                    f"after {lat['tokens_before_escalation']:,} tokens[/]")
            # Completion events carry WHO landed the task (the process-derived
            # `actor`, fleet task 61c219c8) — surface it so a landing is
            # attributable even when several agent sessions share one git
            # identity. `markup=False`: the actor is already sanitised at the
            # write end, this is defence in depth for the whole line.
            for e in events:
                if e.get("kind") in _COMPLETION_EVENT_KINDS:
                    line = f"completion: {e.get('kind')}"
                    if e.get("actor"):
                        line += f" (actor: {e['actor']})"
                    if e.get("text"):
                        line += f" — {e['text']}"
                    console.print(line, markup=False)
            attempts = await store.list_attempts(t.id)
            for a in attempts:
                console.print(
                    f"  attempt {a['attempt_number']}: {a['status']} "
                    f"branch={a['branch_name']} pr={a['pr_url']} "
                    f"turns={a['turns_used']} tests={a['test_results']}"
                )
                # Which code produced this verdict. Printed from the RECORDED
                # column — a pure DB read of what the server stamped at the
                # time, never a measurement taken now. That distinction is the
                # whole point: `nh` runs in its own process, so anything this
                # command measured about ITS OWN checkout would describe the
                # CLI, not the server that judged the attempt. Rows written
                # before this column existed are NULL and print nothing rather
                # than inviting a guess.
                if a.get("loaded_code_version"):
                    console.print(
                        f"    code: {a['loaded_code_version']}"
                    )
            # The surface `_SUMMARY_TRUNCATED_MARKER` (PR body, capped at
            # `_SUMMARY_MAX_CHARS`) now points a reader at. Walk attempts
            # newest-first and print the first non-empty report — same
            # "last attempt that has one" rule as `TaskOut.full_report`.
            # Lazy import: `report_surface` pulls in `orchestrator`, and this
            # CLI module is imported well before any task runs.
            from ..core.report_surface import render_full_report
            for a in reversed(attempts):
                rendered = render_full_report(a.get("full_final_text"))
                if rendered:
                    console.print(
                        f"\n[bold]final report (attempt {a['attempt_number']}, full)[/]"
                    )
                    console.print(rendered, markup=False)
                    break

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Task lifecycle: pause / resume / cancel / retry                              #
# --------------------------------------------------------------------------- #

_PARKED = {TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
           TaskStatus.PAUSED_QUOTA, TaskStatus.ESCALATED}
_ACTIVE_STATES = {TaskStatus.CONTEXT, TaskStatus.PLANNING, TaskStatus.IMPLEMENTING,
                  TaskStatus.REVIEWING, TaskStatus.TESTING}


@task.command("pause")
@click.argument("task_id")
@click.option("--reason", default="user paused via CLI", help="Reason for pausing.")
def task_pause(task_id, reason):
    """Pause a running task. A running attempt stops at its next tool call."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            if t.status in _PARKED or t.status in {TaskStatus.DONE, TaskStatus.FAILED}:
                console.print(
                    f"[yellow]task is {t.status.value}[/] — cannot pause "
                    f"(only active tasks can be paused)"
                )
                return

            # Always raise the flag: it is the only signal a running orchestrator
            # observes, and the only write that cannot race it (single-writer
            # control column, never `context`).
            await store.request_cancel(t.id, reason)

            if _server_owns_worker(config):
                # The server owns this task's status. It will checkpoint the
                # work as [WIP-BLOCKED] and park the task itself; writing the
                # status from here would race the attempt that is still running.
                console.print(
                    f"[yellow]pause requested[/] {t.id[:8]} — the running attempt "
                    f"will checkpoint and stop within a few seconds.\n"
                    f"Watch it: [bold]nh logs {t.id[:8]}[/]"
                )
                return

            # No server: nothing is running, so this process is the only writer.
            # Carry the checkpoint the task already had (twin of the board's
            # direct-park branch; `carried_checkpoint` honours a human's
            # sha-less `resume_from` as a veto).
            from ..blockers import carried_checkpoint, human_event, user_pause_blocker
            prior_status = t.status
            prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
            prior = carried_checkpoint(t) or {}
            t.blocker = user_pause_blocker(reason, checkpoint=prior, paused_by="cli")
            await store.update_task(t)
            await store.set_status(
                t, TaskStatus.BLOCKED,
                event=human_event(
                    "pause", prior_status=prior_status, prior_blocker=prior_blocker,
                    reason=reason, actor="cli"),
            )
            await store.clear_cancel_request(t.id)
            console.print(f"[yellow]paused[/] {t.id[:8]} — resume with: "
                          f"[bold]nh task resume {t.id[:8]}[/]")

    asyncio.run(_go())


@task.command("resume")
@click.argument("task_id")
def task_resume(task_id):
    """Resume a paused/blocked task (sets it to IMPLEMENTING)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            if t.status not in _PARKED:
                console.print(
                    f"[yellow]task is {t.status.value}[/] — only parked tasks "
                    f"(blocked/awaiting_input/paused_quota/escalated) can be resumed"
                )
                return
            # Continue from the checkpoint the blocker recorded, exactly as
            # `nh reply` does. Without this the next attempt branches from a
            # STALE `resume_from` (or from base) and silently throws away the
            # work the parked attempt had already committed. Read it before
            # clearing the blocker, which is what holds the sha.
            from ..blockers import human_event, resume_checkpoint, resume_provenance
            prior_status = t.status
            prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
            checkpoint = resume_checkpoint(t.blocker)
            # Provenance is stamped UNCONDITIONALLY — see `WakeWatcher._resume`.
            # Gating it on the checkpoint left the previous actor's `by`
            # describing this human's resume whenever the blocker recorded no
            # sha, which the honesty gate then read as a machine re-entry.
            t.context = await store.merge_context(
                t.id, {"resume_from": resume_provenance(checkpoint, "human")})

            t.blocker = None
            t.wake_check_at = None
            await store.update_task_columns(t)
            # "Run again" withdraws any pending stop, or the next attempt would
            # honour it immediately and park the task straight back.
            await store.clear_cancel_request(t.id)
            await store.set_status(
                t, TaskStatus.IMPLEMENTING, validate=False,
                event=human_event(
                    "resume", prior_status=prior_status, prior_blocker=prior_blocker,
                    actor="cli"),
            )
            resumed_at = f" from {checkpoint['sha'][:8]}" if checkpoint else ""
            console.print(f"[green]resumed[/] {t.id[:8]}{resumed_at} → implementing")

    asyncio.run(_go())


# The exact false-done signature `restore-approval` repairs on a DONE row:
# a completion event of one of these kinds means the DONE was NOT silent —
# something legitimately completed the task, and the verb must refuse rather
# than second-guess a real completion (task 8c8b36b5's false DONE has none
# of these; its events stop at a `wake_tick`).
_COMPLETION_EVENT_KINDS = frozenset({
    "merged", "shipped", "shipped_pr_closed", "shipped_comment_after_landing",
    "human_merged", "approved_already_satisfied", "review_finished",
    "approved_landed_override",
})


@task.command("restore-approval")
@click.argument("task_id")
@click.option("--reason", default="spurious escalation reversed",
              help="Recorded in the repair event.")
def task_restore_approval(task_id, reason):
    """Return a spuriously-escalated task to awaiting_approval — or repair a
    task false-flipped to DONE with its PR still open and no completion
    event on record.

    Hard-scoped repair, not a generic override: only a task STOPPED on a
    blocker that already opened a PR (pr_open event + pr_watch in context)
    qualifies — the shape of the 2026-07-10 incident where the product's own
    results comment resumed a merge-ready task into the budget gate. The repair
    is recorded as a human_restore_approval event carrying the displaced
    blocker, in the same transaction as the status change.

    Accepts FAILED as well as ESCALATED since 2026-08-09: with
    `budget.exhaustion_terminal` (default on) the budget gate ends the task in
    FAILED, and this repair — which `nh doctor` names by name for exactly that
    incident shape — would otherwise refuse the only status the incident can
    now produce. The narrow guards are unchanged and are what make it safe: an
    open PR and a pr_open event, neither of which a task that never got there
    can fake.

    Also accepts DONE (2026-08-12, task 8c8b36b5's incident class): a row is
    a false-done repair candidate only when EVERY one of these holds — no
    `cancel_reason` (a human's explicit DONE-adjacent call is never touched),
    no completion event on record (`_COMPLETION_EVENT_KINDS` — a real
    completion is never second-guessed), and `task_has_pr_evidence` resolves
    to a real PR (there is genuine outstanding work to restore to). Passing
    all three is exactly the false-done shape; failing any one refuses.

    Also accepts BLOCKED (2026-08-19, the stranded-post-PASS incident): a
    task PAUSED on a blocker after review already PASSED and a PR is open
    (any blocker category qualifies — the precondition is the evidence, not
    what parked it) restores the same way. Refuses when either precondition
    is missing: no PR evidence, or the latest recorded review verdict is not
    a PASS. This is the only route that avoids writing a false `failed`
    state on the way back to awaiting_approval.
    """
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            if t.status not in (TaskStatus.BLOCKED, TaskStatus.ESCALATED,
                                TaskStatus.FAILED, TaskStatus.DONE):
                console.print(f"[yellow]task is {t.status.value!r}, not "
                              "blocked, escalated, failed, or done — nothing to restore[/]")
                sys.exit(1)
            if (t.context or {}).get("cancel_reason"):
                # A CANCELLED task is FAILED by a human's explicit decision —
                # the incident this verb repairs (a parked-PR task wrongly
                # terminal) never produces one. Review-proven: a cancelled
                # task with an old PR passed every guard below, then the
                # terminal CAS silently refused the transition while the
                # "repair" event and blocker wipe went through — a recorded
                # transition that never happened.
                console.print("[yellow]task was cancelled by a human "
                              "(cancel_reason set) — refusing to restore[/]")
                sys.exit(1)
            if t.status is TaskStatus.DONE:
                events = await store.list_events(t.id)
                if any(e.get("kind") in _COMPLETION_EVENT_KINDS for e in events):
                    console.print("[yellow]task has a completion event on "
                                  "record — this DONE was not silent; "
                                  "refusing[/]")
                    sys.exit(1)
                pr_url = await task_has_pr_evidence(store, t)
                if not pr_url:
                    console.print("[yellow]no PR evidence for this task — "
                                  "nothing outstanding to restore[/]")
                    sys.exit(1)
                from ..blockers import human_event
                prior = t.status.value
                prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
                cleared = [k for k in (
                    "approved_at", "already_satisfied_report",
                    "approval_superseded_at",
                ) if k in (t.context or {})]
                event_text = (
                    f"{prior} → awaiting_approval: {reason}; "
                    f"false-done repair (no completion event on "
                    f"record); PR: {pr_url}; cleared context keys: "
                    f"{cleared}")
                moved = await store.set_status(
                    t, TaskStatus.AWAITING_APPROVAL, validate=False,
                    human_override=True,
                    event=human_event(
                        "restore_approval", prior_status=prior,
                        prior_blocker=prior_blocker, reason=reason,
                        actor="cli", text=event_text))
                if moved is None:
                    console.print("[red]the transition was refused by the "
                                  "store (concurrent change?) — nothing was "
                                  "recorded; re-run after checking "
                                  "`nh task show`[/]")
                    sys.exit(1)
                t.context = await store.merge_context(
                    t.id, {"approved_at": None, "already_satisfied_report": None,
                           "approval_superseded_at": None})
                t.blocker = None
                t.wake_check_at = None
                await store.update_task(t)
                console.print(f"[green]{t.id[:8]} → awaiting_approval[/] "
                              f"(false-done repair recorded)")
                return
            if t.status is TaskStatus.BLOCKED:
                # The stranded-post-PASS incident (2026-08-19): a task
                # AWAITING_APPROVAL gets resumed to IMPLEMENTING by a
                # pr_conflict round, is paused there (USER_PAUSED) before the
                # burn finishes, and lands `blocked` with its earlier PASS
                # and open PR both still on record. The precondition is that
                # EVIDENCE, not the blocker category — any `blocked` task
                # qualifies once it has a PR and a passing review verdict.
                pr_url = await task_has_pr_evidence(store, t)
                if not pr_url:
                    console.print("[yellow]blocked task has no PR evidence — "
                                  "restore-approval needs a PR to restore "
                                  "to[/]")
                    sys.exit(1)
                verdict = await store.latest_review_verdict(t.id)
                if verdict is None:
                    console.print("[yellow]no review verdict on record — "
                                  "restore-approval needs a PASSED "
                                  "independent review[/]")
                    sys.exit(1)
                if not verdict:
                    console.print("[yellow]the latest review verdict is a "
                                  "FAIL — refusing[/]")
                    sys.exit(1)
                evidence = "review PASS (verdict on record)"
                # Best-effort tip match: if review_history names which commit
                # passed, prefer evidence naming the branch head — but never
                # fail OPEN into a merge and never fail CLOSED on an
                # unresolvable local repo. `nh approve` re-runs the strict
                # `_review_pass_evidence` head check before it will ever
                # merge (see `approve`, below); this verb only has to put the
                # task back where that gate can run.
                if (t.context or {}).get("review_history"):
                    tip_note = "review tip check: not resolvable"
                    try:
                        from ..vcs.git import GitError, GitRepo
                        from ..vcs.task_pr import resolve_task_pr
                        git_cfg = config.get("git") or {}
                        repo = GitRepo(
                            Path(t.repo_path),
                            identity_name=git_cfg.get(
                                "agent_identity_name", "no_human"),
                            identity_email=git_cfg.get(
                                "agent_identity_email", "no-human@acme.com"),
                            never_push_to=git_cfg.get("never_push_to")
                            or ["main", "master", "release/*"],
                        )
                        repo.fetch()
                        resolved = await resolve_task_pr(store, t)
                        branch = resolved.branch
                        ref = repo.resolve_commitish(branch) if branch else ""
                        head_sha = repo._run("rev-parse", ref) if ref else ""
                        if head_sha:
                            tip_passed, tip_evidence = _review_pass_evidence(
                                t.context or {}, head_sha, repo)
                            if not tip_passed:
                                console.print(
                                    "[yellow]blocked task's review does not "
                                    f"match its branch tip — {tip_evidence}"
                                    "[/]")
                                sys.exit(1)
                            tip_note = tip_evidence
                    except (GitError, OSError):
                        pass
                    evidence = f"{evidence}; {tip_note}"
                # The event is now written by `set_status` itself, in the
                # same transaction as the status write (see `human_event`),
                # so every value it needs must be read off the task BEFORE
                # that call — a silently-refused CAS must never leave an
                # event describing a state change that never happened, and
                # `set_status` already enforces that by only inserting on a
                # transition that actually took.
                from ..blockers import human_event
                prior = t.status.value
                prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
                displaced = str(t.blocker)[:400]
                disarmed_condition = (t.blocker or {}).get("wake_condition")
                disarmed_wake_check_at = t.wake_check_at
                event_text = (
                    f"{prior} → awaiting_approval: {reason}; "
                    f"evidence: {evidence}; PR: {pr_url}; "
                    f"displaced blocker: {displaced}; "
                    f"disarmed wake_condition={disarmed_condition!r} "
                    f"wake_check_at={disarmed_wake_check_at!r}")
                moved = await store.set_status(
                    t, TaskStatus.AWAITING_APPROVAL, validate=False,
                    human_override=True,
                    event=human_event(
                        "restore_approval", prior_status=prior,
                        prior_blocker=prior_blocker, reason=reason,
                        actor="cli", text=event_text))
                if moved is None:
                    console.print("[red]the transition was refused by the "
                                  "store (concurrent change?) — nothing was "
                                  "recorded; re-run after checking "
                                  "`nh task show`[/]")
                    sys.exit(1)
                t.blocker = None
                t.wake_check_at = None
                await store.update_task(t)
                if pr_url:
                    t.context = await store.merge_context(
                        t.id, {"pr_closed_repaired_url": pr_url})
                console.print(f"[green]{t.id[:8]} → awaiting_approval[/] "
                              f"(repair recorded)")
                return
            pr_url = await task_has_pr_evidence(store, t)
            if not pr_url:
                console.print("[yellow]task has no open PR (no PR evidence) — "
                              "restore-approval only repairs parked-PR tasks[/]")
                sys.exit(1)
            events = await store.list_events(t.id)
            if not any(e.get("kind") in PR_EVENT_KINDS for e in events):
                console.print("[yellow]no PR event on record "
                              f"({'/'.join(sorted(PR_EVENT_KINDS))}) — refusing[/]")
                sys.exit(1)
            # The event is written by `set_status` itself, in the same
            # transaction as the status write (see `human_event`), so every
            # value it needs — including what the wake watcher would
            # otherwise still act on — must be read off the task BEFORE that
            # call. Naming what was disarmed in the event, not just dumping
            # the raw (truncated) blocker, is what makes the repair
            # auditable; a silently-refused CAS never gets an event, because
            # `set_status` only inserts on a transition that actually took.
            from ..blockers import human_event
            prior = t.status.value
            prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
            displaced = str(t.blocker)[:400]
            disarmed_condition = (t.blocker or {}).get("wake_condition")
            disarmed_wake_check_at = t.wake_check_at
            event_text = (
                f"{prior} → awaiting_approval: {reason}; "
                f"displaced blocker: {displaced}; "
                f"disarmed wake_condition={disarmed_condition!r} "
                f"wake_check_at={disarmed_wake_check_at!r}")
            moved = await store.set_status(
                t, TaskStatus.AWAITING_APPROVAL, validate=False,
                human_override=True,
                event=human_event(
                    "restore_approval", prior_status=prior,
                    prior_blocker=prior_blocker, reason=reason,
                    actor="cli", text=event_text))
            if moved is None:
                console.print("[red]the transition was refused by the store "
                              "(concurrent change?) — nothing was recorded; "
                              "re-run after checking `nh task show`[/]")
                sys.exit(1)
            t.blocker = None
            t.wake_check_at = None
            await store.update_task(t)
            # Stamp the PR URL as repaired so `WakeWatcher._pr_closed_answered`
            # holds the `pr_closed` rung terminal for it on the very next tick
            # — the fast, context-only path, so the guard does not depend on
            # the wake watcher successfully re-deriving the same answer from
            # this event's text and timestamp (2026-08-12 repair-defeating
            # loop: ESCALATED is not terminal, so an unguarded rung re-fired
            # within one poll interval of this exact repair).
            if pr_url:
                t.context = await store.merge_context(
                    t.id, {"pr_closed_repaired_url": pr_url})
            console.print(f"[green]{t.id[:8]} → awaiting_approval[/] "
                          f"(repair recorded)")

    asyncio.run(_go())


@task.command("cancel")
@click.argument("task_id")
@click.option("--reason", default="cancelled by user", help="Reason for cancelling.")
def task_cancel(task_id, reason):
    """Cancel a task (sets it to FAILED with reason)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            if t.status is TaskStatus.DONE:
                console.print(f"[yellow]task is already {t.status.value}[/]")
                return
            if t.status is TaskStatus.FAILED:
                prior_reason = (t.context or {}).get("cancel_reason")
                if prior_reason:
                    console.print(
                        f"[yellow]{t.id[:8]} is already cancelled[/] — "
                        f"reason: {prior_reason} (no change)")
                    return
                # A FAILED task with no `cancel_reason` is a REAL failure, not
                # a human cancel — a human asserting "call this cancelled" is
                # a re-labelling, recorded the same way `nh approve --landed`
                # records an assertion: honestly, with a human event, and
                # without pretending automated evidence produced it.
                from ..blockers import human_event
                t.context = await store.record_cancel_reason(t.id, reason)
                await store.save_events(t.id, [{
                    **human_event(
                        "cancel", prior_status=TaskStatus.FAILED,
                        prior_blocker=(t.blocker if isinstance(t.blocker, dict)
                                       else None),
                        reason=reason, actor="cli",
                        text=f"human re-labelled a failed task as cancelled: "
                             f"{reason}"),
                    "prior_cancel_reason": prior_reason,
                    "ts": time.time(),
                }])
                console.print(
                    f"[red]cancelled[/] {t.id[:8]} — reason: {reason} "
                    f"(was failed)")
                return

            # Raise the stop flag first: it is the signal a running attempt sees.
            await store.request_cancel(t.id, reason)

            if _server_owns_worker(config) and t.status in _ACTIVE_STATES:
                # The attempt is mid-flight and the server owns the status.
                # POST to the server's own cancel endpoint first — it hard-
                # stops a live coder session within one scheduler tick instead
                # of only waiting on the cooperative flag raised above (which
                # a backend that never emits an SDK event would never notice)
                # — and it is what flips the task to FAILED with its
                # checkpoint, so no second "again once it has stopped" call is
                # needed. Falls back to the old cooperative-only message when
                # there is no HTTP endpoint to reach (`nh serve` binds none).
                if _post_server_cancel(config, t.id, reason):
                    console.print(
                        f"[red]cancelled[/] {t.id[:8]} — the server stopped "
                        f"the running session and closed the attempt."
                    )
                    return
                console.print(
                    f"[yellow]cancel requested[/] {t.id[:8]} — the running attempt "
                    f"will checkpoint and stop within a few seconds, then park as "
                    f"blocked.\nMark it dead with [bold]nh task cancel {t.id[:8]}[/] "
                    f"again once it has stopped."
                )
                return

            from ..blockers import human_event
            prior_status = t.status
            prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
            t.context = await store.record_cancel_reason(t.id, reason)
            await store.clear_cancel_request(t.id)
            await store.set_status(
                t, TaskStatus.FAILED, validate=False, human_override=True,
                event=human_event(
                    "cancel", prior_status=prior_status, prior_blocker=prior_blocker,
                    reason=reason, actor="cli"),
            )
            console.print(f"[red]cancelled[/] {t.id[:8]} — reason: {reason}")

    asyncio.run(_go())


@task.command("retry")
@click.argument("task_id")
def task_retry(task_id):
    """Retry a failed task (resets to PENDING for a fresh run)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            if t.status != TaskStatus.FAILED:
                console.print(
                    f"[yellow]task is {t.status.value}[/] — only failed tasks "
                    f"can be retried. Use [bold]nh task resume[/] for parked tasks."
                )
                return
            prior_status = t.status
            prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
            t.blocker = None
            t.wake_check_at = None
            # None deletes the key (RFC 7396) — clears cancel_reason atomically.
            #
            # `resume_from` goes with it, exactly as in the twin endpoint
            # `POST /api/tasks/{id}/retry`. A "fresh run" must not inherit a
            # checkpoint some EARLIER actor chose: the zero-diff honesty gate
            # reads that pair, and a stale `by: "human"` disarms it for a run
            # nobody gated — an attempt that edits nothing is then credited and
            # a PR opens on work no attempt produced.
            #
            # 🔴 This twin was missed when the endpoint was fixed, and that is
            # the FOURTH time in this branch a fix landed on one of a pair:
            # `nh reply` behind the reply endpoint, `nh unblock` behind the
            # Resume endpoint's guards, and now here. When a CLI verb and an
            # HTTP endpoint share a docstring, they share an invariant.
            # Dropping `resume_from` is not enough on its own: the orphan sweep
            # re-derives a checkpoint from the attempt row still `in_progress`,
            # so the first sweep after this retry put the cleared checkpoint
            # straight back. Retire those rows first — see
            # `Store.close_open_attempts` for why the distinction has to be
            # made here and cannot be made by the sweep.
            from ..blockers import human_event
            await store.close_open_attempts(t.id)
            t.context = await store.merge_context(
                t.id, {"cancel_reason": None, "retried_at": _now_iso(),
                       "resume_from": None})
            await store.update_task_columns(t)
            await store.clear_cancel_request(t.id)
            await store.set_status(
                t, TaskStatus.PENDING, validate=False, human_override=True,
                event=human_event(
                    "retry", prior_status=prior_status,
                    prior_blocker=prior_blocker, actor="cli"),
            )
            console.print(f"[green]retried[/] {t.id[:8]} → pending (will run on next dispatch)")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Config management                                                            #
# --------------------------------------------------------------------------- #

@cli.command("config")
@click.argument("action", type=click.Choice(["show", "edit", "path", "models"]))
@click.option("--key", default=None, help="Show a specific config key (dot-separated).")
@click.argument("extra", nargs=-1)
def config_cmd(action, key, extra):
    """Show, edit, or locate the config file; view or change the model picker.

    \b
      nh config show                       # pretty-print full config
      nh config show --key git             # show just the git section
      nh config edit                       # open in $EDITOR
      nh config path                       # print the config file path
      nh config models                     # show the model picker (5 roles)
      nh config models set coder <id>      # change one role's model
    """
    import yaml as _yaml
    from ..config import CONFIG_PATH as _cfg_path

    if action == "path":
        console.print(str(_cfg_path))
        return

    if action == "edit":
        editor = os.environ.get("EDITOR", "vi")
        import subprocess as _sp
        _sp.run([editor, str(_cfg_path)])
        return

    if action == "models":
        _config_models_cmd(extra)
        return

    # action == "show"
    if not _cfg_path.exists():
        console.print(f"[yellow]no config file at {_cfg_path}[/]\n"
                       "Run [bold]nh init[/] to create one.")
        return
    data = _yaml.safe_load(_cfg_path.read_text()) or {}
    if key:
        parts = key.split(".")
        node = data
        for p in parts:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                console.print(f"[red]key not found:[/] {key}")
                return
        data = {key: node}
    console.print_json(data=data)


def _config_models_cmd(extra: tuple[str, ...]) -> None:
    """`nh config models` / `nh config models set <role> <id>` — the exact
    same validate-then-write path `PUT /api/config/models` uses
    (`core.model_settings`), so the CLI and the Settings UI can never
    disagree about what is allowed or how a write is persisted.
    """
    from ..config import CONFIG_PATH, load_config
    from ..core import model_catalog as mc
    from ..core import model_settings

    on_disk = load_config(CONFIG_PATH)

    if not extra:
        payload = model_settings.models_payload(on_disk.data, CONFIG_PATH)
        for row in payload["roles"]:
            marker = " [yellow](restart required to take effect)[/]" if (
                payload["restart_required"]) else ""
            console.print(f"[bold]{row['role']}[/] ({row['key']}): "
                          f"{row['current']}{marker if row['current'] != row['default'] else ''}")
            for opt in row["options"]:
                tag = " [default]" if opt["is_default"] else ""
                backend_tag = " [needs worker.backend]" if opt["requires_backend"] else ""
                console.print(f"    {opt['id']}  ({opt['price_class']['label']})"
                              f"{tag}{backend_tag}")
        return

    if len(extra) != 3 or extra[0] != "set":
        raise click.ClickException(
            "usage: nh config models set <role> <model-id> — role is one of "
            f"{sorted(mc.ROLES)}")
    _, role, model_id = extra
    if role not in mc.ROLES:
        raise click.ClickException(f"unknown role {role!r}; must be one of {sorted(mc.ROLES)}")
    config_key = mc.ROLES[role]

    try:
        payload, changes = model_settings.apply_model_changes(
            {config_key: model_id},
            running_cfg_data=on_disk.data,
            config_path=CONFIG_PATH,
        )
    except (model_settings.ModelSettingsError, AuthError) as exc:
        raise click.ClickException(str(exc)) from exc

    if not changes:
        console.print(f"[yellow]no change[/] — {role} is already {model_id}")
        return

    async def _persist_event() -> None:
        cfg = load_config(CONFIG_PATH)
        async with Store(cfg.db_path) as store:
            await store.save_events(
                model_settings.CONFIG_AUDIT_TASK_ID,
                [model_settings.model_change_event(changes)],
            )

    asyncio.run(_persist_event())

    change = changes[config_key]
    console.print(f"[green]{role}[/] ({config_key}): {change['old']} -> {change['new']}")
    console.print("[yellow]restart required[/] for the running server to pick this up.")


# --------------------------------------------------------------------------- #
# Auth profiles — which subscription pays                                      #
# --------------------------------------------------------------------------- #


@cli.group("auth")
def auth_group():
    """Which Claude subscription pays for a run.

    Tokens live only in ~/.no_human/.env (chmod 600). These commands print
    profile names and whether a token is present — never a token value.
    """


@auth_group.command("status")
def auth_status():
    """Show the active auth profile and which profiles have a token."""
    from ..config import (
        DEFAULT_AUTH_PROFILE,
        available_auth_profiles,
        profile_token_var,
    )

    # Deliberately not _bootstrap(): a diagnostic must still print when the
    # active profile's token is missing, which is exactly when it is needed.
    config = load_config()
    active = (config.get("llm") or {}).get("auth_profile") or DEFAULT_AUTH_PROFILE
    var = profile_token_var(active)
    available = available_auth_profiles()

    table = Table(show_header=False, box=None)
    table.add_row("active profile", f"[bold]{active}[/]")
    table.add_row("token variable", var)
    table.add_row(
        "token present", "[green]yes[/]" if active in available else "[red]no[/]"
    )
    table.add_row("profiles with a token", ", ".join(available) or "[dim]none[/]")
    console.print(table)

    if active not in available:
        console.print(
            f"\n[bold red]The active profile has no token.[/] Add [bold]{var}[/] "
            f"to ~/.no_human/.env, or switch:  [bold]nh auth use <profile>[/]"
        )
    if _server_owns_worker(config):
        console.print(
            "\n[dim]A server is running; it bills the profile it started with.[/]"
        )


@auth_group.command("set-token")
@click.option("--profile", default=None,
              help="Which profile's token to write (default: the active one).")
def auth_set_token(profile):
    """Replace a profile's OAuth token, reading it from stdin.

    The CLI twin of the Mac app's *File → Re-enter Claude Token…*. Until this
    existed a mistyped token could not be fixed from the CLI at all: `nh init`
    short-circuits on the PRESENCE of a credential and never overwrites one, so
    the only remedy was hand-editing ~/.no_human/.env (walkthrough B14b).

    The token is read from stdin, never taken as an argument — argv shows up in
    `ps` and in shell history. Piped in, it is one line; typed at a terminal,
    it is prompted for without echo.

    \b
      printf %s "$TOKEN" | nh auth set-token
      nh auth set-token --profile personal      # then paste when prompted
    """
    from ..config import DEFAULT_AUTH_PROFILE, profile_token_var, set_profile_token

    config = load_config()
    target = profile or (config.get("llm") or {}).get(
        "auth_profile") or DEFAULT_AUTH_PROFILE
    # A bad profile name must fail BEFORE a credential is read, so a rejected
    # run never has the token in memory at all.
    try:
        var = profile_token_var(target)
    except AuthError as exc:
        console.print(f"[bold red]auth error:[/] {exc}")
        sys.exit(2)

    # getpass would read /dev/tty when one exists, which silently ignores a
    # pipe; readline would hang with no prompt on a terminal. Branch, and each
    # half is correct for the case it serves.
    if sys.stdin.isatty():
        token = click.prompt(var, hide_input=True, default="",
                             show_default=False)
    else:
        token = sys.stdin.readline()

    try:
        set_profile_token(target, token)
    except AuthError as exc:
        console.print(f"[bold red]not saved —[/] {exc}")
        sys.exit(2)

    console.print(f"[green]✓[/] wrote [bold]{var}[/] "
                  f"(profile [bold]{target}[/]) to ~/.no_human/.env")
    if _server_owns_worker(config):
        console.print(
            "[yellow]The running server still holds the old token.[/] "
            "Restart it (`nh stop && nh start`) for this to take effect."
        )


@auth_group.command("use")
@click.argument("profile")
def auth_use(profile):
    """Pin the auth profile that future runs bill. Requires a server restart.

    \b
      nh auth use personal      # bills CLAUDE_CODE_OAUTH_TOKEN_PERSONAL
      nh auth use default       # bills the unsuffixed CLAUDE_CODE_OAUTH_TOKEN
    """
    from ..config import available_auth_profiles, profile_token_var, set_auth_profile

    normalized = profile.strip().lower()
    available = available_auth_profiles()
    if normalized not in available:
        console.print(
            f"[bold red]no token for profile[/] '{normalized}'. Expected "
            f"[bold]{profile_token_var(normalized)}[/] in ~/.no_human/.env.\n"
            f"Profiles with a token: {', '.join(available) or 'none'}"
        )
        sys.exit(2)

    try:
        set_auth_profile(normalized)
    except AuthError as exc:
        console.print(f"[bold red]auth error:[/] {exc}")
        sys.exit(2)

    console.print(f"[green]✓[/] auth profile set to [bold]{normalized}[/]")
    if _server_owns_worker(load_config()):
        console.print(
            "[yellow]The running server still bills its startup profile.[/] "
            "Restart it (`nh stop && nh start`) for this to take effect — a "
            "live task is never re-billed mid-run."
        )


# --------------------------------------------------------------------------- #
# Rules management (Phase G)                                                   #
# --------------------------------------------------------------------------- #


@cli.group("rules")
def rules_group():
    """Manage the confirmed rule set (anti-patterns + constraints)."""


@rules_group.command("list")
def rules_list():
    """List all confirmed rules."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_RULE, TYPE_ANTI_PATTERN
            items = await store.list_memories(confirmed=True, mem_type=TYPE_RULE)
            items += await store.list_memories(confirmed=True, mem_type=TYPE_ANTI_PATTERN)
            if not items:
                console.print("[dim]no confirmed rules yet[/]\n"
                              "Add one: [bold]nh rules add --title '...' --content '...'[/]")
                return
            table = Table(title="Confirmed rules")
            table.add_column("id", style="dim", no_wrap=True)
            table.add_column("type")
            table.add_column("title")
            table.add_column("tags", style="dim")
            for m in items:
                import json as _json
                tags = ", ".join(_json.loads(m.get("tags") or "[]"))
                table.add_row(m["id"][:8], m["type"], m["title"][:60], tags[:40])
            console.print(table)

    asyncio.run(_go())


@rules_group.command("add")
@click.option("--title", required=True, help="Short rule title.")
@click.option("--content", required=True, help="Rule content / description.")
@click.option("--tag", multiple=True, help="Tags (can be repeated).")
@click.option("--project", default=None, help="Repo path this rule applies to.")
def rules_add(title, content, tag, project):
    """Add a confirmed rule directly (skips the learning queue)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_RULE
            mem_id = await store.add_memory(
                mem_type=TYPE_RULE, title=title, content=content,
                tags=list(tag), project=project,
                source="manual", confirmed=True,
            )
            if mem_id:
                console.print(f"[green]added[/] rule {mem_id[:8]}: {title}")
            else:
                console.print("[yellow]duplicate — a rule with the same content exists[/]")

    asyncio.run(_go())


@rules_group.command("remove")
@click.argument("rule_id")
def rules_remove(rule_id):
    """Remove a rule by ID prefix."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            m = await store.find_memory(rule_id)
            if not m:
                console.print(f"[red]no rule matching[/] {rule_id}")
                sys.exit(1)
            await store.delete_memory(m["id"])
            console.print(f"[red]removed[/] {m['id'][:8]}: {escape(str(m['title']))}",
                          emoji=False)

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# P1 brain hygiene — the memories-table inventory + one-time backfill         #
# --------------------------------------------------------------------------- #

@cli.group("memories")
def memories_group():
    """P1 brain hygiene: inventory and quarantine of employer-context rows in
    the memories table (`learning/provenance.py`)."""


@memories_group.command("scan")
@click.option("--apply", "apply_", is_flag=True,
              help="Flag the union set quarantined=1 (idempotent; never clears).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def memories_scan(apply_, as_json):
    """Report the needle-class inventory; optionally backfill the quarantine
    flag onto the matched rows.

    Reports CLASS LABELS and counts only — never a matched term, never a row
    title or content excerpt. A scan that reads zero rows while the UI lists
    confirmed rules/skills is a FAILURE (`InventoryError`, non-zero exit), not
    a clean run — see `learning.provenance.scan_memories` for why.

    Counts are also reported BY CLASS INDEX (``class-1``, ``class-2``, ...;
    ``needle-1``, ``needle-2``, ...) — the only form of this inventory safe to
    quote on a forge-visible surface (PR body, commit message). Class labels
    stay local to this console/JSON output and never leave the machine."""
    if as_json:
        mark_machine_output()
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        from ..learning.provenance import (
            InventoryError, project_allowlist, quarantine_reason, scan_memories,
        )
        allowlist = project_allowlist(config)
        async with Store(config.db_path) as store:
            try:
                inv = await scan_memories(store, allowlist=allowlist)
            except InventoryError as exc:
                if as_json:
                    click.echo(json.dumps({"error": str(exc)}))
                else:
                    console.print(f"[bold red]scan failed:[/] {exc}")
                sys.exit(1)
            newly_flagged = 0
            if apply_:
                for mem_id in inv.union_ids:
                    row = await store.find_memory(mem_id)
                    if row is None or int(row.get("quarantined") or 0):
                        continue
                    reason = quarantine_reason(
                        title=row.get("title"), content=row.get("content"),
                        project=row.get("project"), allowlist=allowlist,
                    )
                    if await store.set_quarantine(mem_id, True, reason):
                        newly_flagged += 1
            result = {
                "total_rows": inv.total_rows,
                "per_class": inv.per_class,
                "per_class_index": inv.per_class_index,
                "per_needle_index": inv.per_needle_index,
                "union_total": inv.union_total,
                "applied": apply_,
                "newly_flagged": newly_flagged,
            }
            if as_json:
                click.echo(json.dumps(result))
                return
            console.print(f"scanned [bold]{inv.total_rows}[/] row(s)")
            for i in sorted(inv.per_class_index):
                console.print(f"  class-{i}: {inv.per_class_index[i]}")
            for i in sorted(inv.per_needle_index):
                console.print(f"  class-2/needle-{i}: {inv.per_needle_index[i]}")
            for cls, n in inv.per_class.items():
                console.print(f"  {cls}: {n}")
            console.print(f"union total: [bold]{inv.union_total}[/]")
            if apply_:
                console.print(f"newly flagged quarantined: {newly_flagged}")
            else:
                console.print("[dim]report only — pass --apply to flag the "
                               "union set quarantined=1[/]")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Playbooks (1.4) — reusable procedures injected when a task matches           #
# --------------------------------------------------------------------------- #

@cli.group("playbook")
def playbook_group():
    """Manage operator playbooks (Procedure / Postconditions / Forbidden /
    Required). A playbook is injected into the coder prompt only when one of its
    trigger keywords appears in the task text."""


@playbook_group.command("list")
def playbook_list():
    """List all playbooks."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            items = await store.list_playbooks()
            if not items:
                console.print("[dim]no playbooks yet[/]\nAdd one: [bold]nh "
                              "playbook add --title '...' --trigger stripe "
                              "--procedure '...' --postcondition '...'[/]")
                return
            import json as _json
            table = Table(title="Playbooks")
            table.add_column("id", style="dim", no_wrap=True)
            table.add_column("title")
            table.add_column("triggers", style="dim")
            table.add_column("project", style="dim")
            for p in items:
                trg = ", ".join(_json.loads(p.get("trigger_keywords") or "[]"))
                table.add_row(p["id"][:8], p["title"][:50], trg[:40],
                              (p.get("project") or "global")[:30])
            console.print(table)

    asyncio.run(_go())


@playbook_group.command("add")
@click.option("--title", required=True, help="Short playbook title.")
@click.option("--trigger", "trigger", multiple=True,
              help="Keyword that triggers this playbook (repeatable). No "
                   "trigger = never auto-injected.")
@click.option("--procedure", default="", help="Step-by-step procedure.")
@click.option("--postcondition", "postcondition", multiple=True,
              help="A condition that must be TRUE when done (repeatable).")
@click.option("--forbidden", "forbidden", multiple=True,
              help="A forbidden action / hard stop (repeatable).")
@click.option("--require", "require", multiple=True,
              help="Something required from the operator up front (repeatable).")
@click.option("--project", default=None, help="Repo path to scope to (else global).")
def playbook_add(title, trigger, procedure, postcondition, forbidden, require, project):
    """Add an operator playbook."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            pb_id = await store.add_playbook(
                title=title, trigger_keywords=list(trigger), procedure=procedure,
                postconditions=list(postcondition), forbidden=list(forbidden),
                required_from_user=list(require), project=project,
            )
            console.print(f"[green]added[/] playbook {pb_id[:8]}: {title}")
            if not trigger:
                console.print("[yellow]note:[/] no --trigger given, so this "
                              "playbook will never auto-inject. Add one to activate it.")

    asyncio.run(_go())


@playbook_group.command("remove")
@click.argument("playbook_id")
def playbook_remove(playbook_id):
    """Remove a playbook by ID prefix."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            if await store.delete_playbook(playbook_id):
                console.print(f"[red]removed[/] playbook {playbook_id}")
            else:
                console.print(f"[red]no playbook matching[/] {playbook_id}")
                sys.exit(1)

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Stacked-PR ordered merge (2.2) — operator-invoked; the agent never merges     #
# --------------------------------------------------------------------------- #

@cli.group("merge-stack")
def merge_stack_group():
    """Merge a chain of DEPENDENT PRs in the correct order. Record edges with
    `link`, see the order with `plan`, execute with `run`. The agent never
    merges: it opens the PRs and stops. YOU run this."""


@merge_stack_group.command("link")
@click.argument("child_pr")
@click.argument("parent_pr")
@click.option("--project", default=None, help="Repo path scope.")
def merge_stack_link(child_pr, parent_pr, project):
    """Record that CHILD_PR must merge AFTER PARENT_PR."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            await store.add_pr_edge(child_pr=child_pr, parent_pr=parent_pr,
                                    project=project)
            console.print(f"[green]linked[/] {child_pr}\n  ⤷ merges after {parent_pr}")

    asyncio.run(_go())


@merge_stack_group.command("plan")
@click.option("--project", default=None, help="Repo path scope.")
def merge_stack_plan(project):
    """Show the safe merge order and which PRs are ready right now."""
    from ..vcs.merge_order import MergeCycle, merge_order, ready_to_merge
    from ..vcs.pr_watcher import default_pr_merged
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            edges = await store.list_pr_edges(project=project)
            if not edges:
                console.print("[dim]no PR edges — link some with "
                              "[bold]nh merge-stack link <child> <parent>[/][/]")
                return
            try:
                order = merge_order(edges)
            except MergeCycle as exc:
                console.print(f"[red]cannot order:[/] {exc}")
                sys.exit(1)
            merged = set()
            for pr in order:
                if await default_pr_merged(pr):
                    merged.add(pr)
            ready = set(ready_to_merge(edges, merged))
            console.rule("[bold]merge order")
            for i, pr in enumerate(order, 1):
                if pr in merged:
                    tag = "[green]merged[/]"
                elif pr in ready:
                    tag = "[bold yellow]READY[/]"
                else:
                    tag = "[dim]blocked (parent not merged)[/]"
                console.print(f"  {i}. {pr}  {tag}")

    asyncio.run(_go())


@merge_stack_group.command("run")
@click.option("--project", default=None, help="Repo path scope.")
@click.option("--squash", is_flag=True, help="Squash-merge (else a merge commit).")
@click.confirmation_option(prompt="Merge the READY PRs in order now?")
def merge_stack_run(project, squash):
    """Merge the currently-ready PRs (parents merged) in topological order via
    `gh`. Stops at the first PR that isn't cleanly mergeable (e.g. needs a
    rebase) and reports it. Operator action — never run by the agent."""
    _refuse_agent_gate_act("merge_stack_run")
    import subprocess
    from ..vcs.merge_order import MergeCycle, merge_order, ready_to_merge
    from ..vcs.pr_watcher import default_pr_merged, parse_pr_url
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            edges = await store.list_pr_edges(project=project)
            if not edges:
                console.print("[dim]no PR edges to merge[/]")
                return
            try:
                order = merge_order(edges)
            except MergeCycle as exc:
                console.print(f"[red]cannot order:[/] {exc}")
                sys.exit(1)
            merged = {pr for pr in order if await default_pr_merged(pr)}
            method = "--squash" if squash else "--merge"
            for pr in order:  # topo order → a parent is always attempted first
                if pr in merged:
                    continue
                if pr not in set(ready_to_merge(edges, merged)):
                    console.print(f"[dim]stop:[/] {pr} is blocked (parent not merged)")
                    break
                parsed = parse_pr_url(pr)
                if not parsed:
                    console.print(f"[red]skip[/] unparseable PR: {pr}")
                    break
                console.print(f"[bold]merging[/] {pr} …")
                proc = subprocess.run(["gh", "pr", "merge", pr, method], capture_output=True, text=True)
                if proc.returncode != 0:
                    console.print(f"[red]merge failed[/] (needs a rebase or CI is "
                                  f"red): {proc.stderr.strip()[:200]}")
                    console.print("  resolve it, then re-run `nh merge-stack run`.")
                    break
                merged.add(pr)
                await store.delete_pr_edges_for(pr)
                console.print(f"  [green]merged[/] {pr}")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Skills management (Phase G)                                                  #
# --------------------------------------------------------------------------- #


@cli.group("skills")
def skills_group():
    """Manage the confirmed skill set (reusable approaches)."""


@skills_group.command("list")
def skills_list():
    """List all confirmed skills."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_SKILL, TYPE_FACT
            items = await store.list_memories(confirmed=True, mem_type=TYPE_SKILL)
            items += await store.list_memories(confirmed=True, mem_type=TYPE_FACT)
            if not items:
                console.print("[dim]no confirmed skills yet[/]\n"
                              "Add one: [bold]nh skills add --title '...' --content '...'[/]")
                return
            table = Table(title="Confirmed skills")
            table.add_column("id", style="dim", no_wrap=True)
            table.add_column("type")
            table.add_column("title")
            table.add_column("tags", style="dim")
            for m in items:
                import json as _json
                tags = ", ".join(_json.loads(m.get("tags") or "[]"))
                table.add_row(m["id"][:8], m["type"], m["title"][:60], tags[:40])
            console.print(table)

    asyncio.run(_go())


@skills_group.command("add")
@click.option("--title", required=True, help="Short skill title.")
@click.option("--content", required=True, help="Skill content / how-to.")
@click.option("--tag", multiple=True, help="Tags (can be repeated).")
@click.option("--project", default=None, help="Repo path this skill applies to.")
def skills_add(title, content, tag, project):
    """Add a confirmed skill directly (skips the learning queue)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_SKILL
            mem_id = await store.add_memory(
                mem_type=TYPE_SKILL, title=title, content=content,
                tags=list(tag), project=project,
                source="manual", confirmed=True,
            )
            if mem_id:
                console.print(f"[green]added[/] skill {mem_id[:8]}: {title}")
            else:
                console.print("[yellow]duplicate — a skill with the same content exists[/]")

    asyncio.run(_go())


@skills_group.command("remove")
@click.argument("skill_id")
def skills_remove(skill_id):
    """Remove a skill by ID prefix."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            m = await store.find_memory(skill_id)
            if not m:
                console.print(f"[red]no skill matching[/] {skill_id}")
                sys.exit(1)
            await store.delete_memory(m["id"])
            console.print(f"[red]removed[/] {m['id'][:8]}: {escape(str(m['title']))}",
                          emoji=False)

    asyncio.run(_go())


@skills_group.command("propose")
@click.option("--title", required=True, help="Short skill title.")
@click.option("--content", required=True, help="Skill content / how-to.")
@click.option("--tag", multiple=True, help="Tags (can be repeated).")
@click.option("--project", default=None, help="Repo path this skill applies to.")
def skills_propose(title, content, tag, project):
    """Propose a skill discovered mid-task — for the agent to call via Bash.

    Queued exactly like `nh learnings`' post-task proposals (source=proposed,
    confirmed=False): never auto-trusted, never delivered to any task until a
    human runs `nh learnings --confirm`. This only widens WHO can propose
    (mid-task, not just post-task) — it does not weaken the confirm gate.
    """
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            from ..learning import TYPE_SKILL
            mem_id = await store.add_memory(
                mem_type=TYPE_SKILL, title=title, content=content,
                tags=list(tag), project=project,
                source="proposed", confirmed=False,
            )
            if mem_id:
                console.print(
                    f"[yellow]proposed[/] skill {mem_id[:8]}: {title}\n"
                    f"Queued for human review — confirm with "
                    f"[bold]nh learnings --confirm {mem_id[:8]}[/]"
                )
            else:
                console.print("[yellow]duplicate — a matching proposal already exists[/]")

    asyncio.run(_go())


async def _follow_task(store, task_id: str, *, poll_s: float = 1.0,
                       echo=None) -> "Task | None":
    """Stream a task's persisted events until it leaves the active statuses.

    The read-only half of `nh watch`, used when a running server owns the
    worker pool: `nh watch` must NEVER drive a second orchestrator beside
    the server's (two coders, two reviewers, two PRs on one checkout — and
    since the runtime stranded sweep judges liveness from outside, a silent
    duplicate is also what the sweep exists to requeue). Follows by polling
    the same event table the server flushes to."""
    echo = echo or render_event
    seen = 0
    active = {TaskStatus.PENDING, TaskStatus.CONTEXT, TaskStatus.PLANNING,
              TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING,
              TaskStatus.TESTING}
    t = await store.find_task(task_id)
    if t is None:
        return None
    while True:
        # ponytail: full re-read + slice each poll; add a since-cursor on
        # list_events if a follow of a many-thousand-event task ever matters.
        events = await store.list_events(t.id)
        for ev in events[seen:]:
            echo(ev)
        seen = len(events)
        t = await store.find_task(t.id)
        if t is None:
            return None            # row deleted mid-follow — stop, don't spin
        if t.status not in active:
            return t
        await asyncio.sleep(poll_s)


@cli.command("watch")
@click.argument("task_id")
def watch(task_id):
    """Watch a task. With `nh start` running, follows the server's run
    read-only; otherwise runs the task in the live Textual TUI."""
    config, _ = _bootstrap()
    if _server_owns_worker(config):
        # The server's scheduler owns the pool — running the task HERE would
        # put two orchestrators on one checkout (task 84251cb2's duplicate
        # PR), and the server's stranded sweep may requeue whichever copy
        # goes silent. Follow the server's run instead.
        console.print("[cyan]a running server owns this task[/] — following "
                      "its events (read-only)")

        async def _go():
            async with Store(config.db_path) as store:
                t = await _follow_task(store, task_id)
                if t is None:
                    print_no_task_matching(task_id)
                    sys.exit(1)
                console.print(f"[bold]── {t.status.value} ──[/]")

        asyncio.run(_go())
        return
    from .tui import run_watch  # lazy import: Textual is heavy
    run_watch(config, task_id)


@cli.command("mcp-serve")
def mcp_serve():
    """Run the MCP stdio bridge (task_add + task_status only, SCRUM-63).

    Talks to the local HTTP API at `server.host`:`server.port` (127.0.0.1:8420 by
    default) — start `nh serve` or `nh start` first. Refuses to start if unreachable.
    """
    from ..intake.mcp_bridge import main as mcp_bridge_main
    mcp_bridge_main()


@cli.command("onboard")
@click.argument("repo", type=click.Path(exists=True))
@click.option("--confirm", is_flag=True,
              help="Confirm the proven profile (the one-click human gate).")
@click.option("--agent", is_flag=True,
              help="Use the agentic read-only recon deriver (for nonstandard repos).")
def onboard(repo, confirm, agent):
    """Derive a repo's install/test/lint commands from its OWN declarations and
    PROVE them by running each, then propose a ProjectProfile for your confirm.

    A profile drives a task only once you confirm it AND its test command was
    proven to run:
      nh onboard ~/repo            # derive + prove + propose
      nh onboard ~/repo --confirm  # confirm the proven profile
    """
    # Auth (subscription) is only needed for the agentic deriver, which runs the
    # backend; the deterministic deriver + subprocess proving need no token.
    config, _ = _bootstrap(require_auth=agent)
    repo_path = str(Path(repo).resolve())
    from ..onboard import (
        AgentDeriver, DeclarationDeriver, OnboardEngine, ProfileNotProven,
        ProjectYmlPersistError, confirm_profile, offer_ui_evidence,
        ui_evidence_suggestion,
    )
    from ..profile import ProjectProfile

    async def _go():
        async with Store(config.db_path) as store:
            if confirm:
                prof = await store.get_profile(repo_path) or ProjectProfile.load(repo_path)
                if not prof:
                    console.print("[red]no profile to confirm[/] — run "
                                  f"[bold]nh onboard {repo}[/] first")
                    sys.exit(1)
                # The gate lives in onboard.confirm_profile so the CLI and the
                # web wizard's confirm step cannot drift apart on what may be
                # confirmed (they used to have separate copies of this check).
                try:
                    confirm_profile(prof)
                except ProfileNotProven as exc:
                    console.print(f"[red]{exc}[/]")
                    sys.exit(1)
                prof.save()
                await store.upsert_profile(prof)
                console.print(f"[bold green]confirmed[/] profile for {repo_path}")
                console.print(f"  usable: {prof.is_usable}  test: [bold]{prof.test_cmd}[/]")
                return

            if agent:
                # Recon must not mutate the repo: a read-only backend so its
                # PreToolUse guard blocks all write tools.
                recon_backend = ClaudeBackend(
                    model=config.primary_model,
                    forbidden_paths=config["safety"]["forbidden_paths"],
                    never_push_to=config["git"]["never_push_to"],
                    readonly=True,
                )
                deriver = AgentDeriver(recon_backend)
            else:
                deriver = DeclarationDeriver()
            console.rule(f"[bold]onboarding {repo_path}")
            console.print(f"[blue]deriving[/] commands from the repo's declarations"
                          f"{' (agentic recon)' if agent else ''} …")
            github_hosts = config["git"].get("github_hosts", ["github.com"])
            result = await OnboardEngine(deriver, github_hosts=github_hosts).onboard(repo_path)
            prof = result.profile

            console.print(f"\n[bold]ecosystem:[/] {prof.ecosystem or '[dim]unknown[/]'}"
                          f"   [dim]derived from: {', '.join(prof.derived_from) or '—'}[/]")
            if prof.vcs_host:
                console.print(f"[bold]vcs:[/] {prof.vcs_host}  [dim]{prof.vcs_remote}[/]")
            console.print("[bold]proving (running each candidate):[/]")
            for p in result.proofs:
                icon = "[green]✓[/]" if p.ok else "[red]✗[/]"
                console.print(f"  {icon} {p.summary}")
            console.print("\n[bold]proposed profile:[/]")
            for label, val in (("install", prof.install_cmd), ("test", prof.test_cmd),
                               ("lint", prof.lint_cmd)):
                proven = prof.proven.get(f"{label}_cmd")
                tag = "[green](proven)[/]" if proven else "[dim](unproven)[/]" if val else ""
                console.print(f"  {label}: {val or '[dim]—[/]'} {tag}")
            if prof.ci:
                console.print(f"  ci: {prof.ci}")
            if prof.human_gated_steps:
                console.print(f"  human-gated: {prof.human_gated_steps}")

            # Credential preflight (WS-F): show which .env keys this repo needs
            # and which are still missing — never the values.
            if prof.required_credentials:
                from ..config import credential_status
                status = credential_status(prof.required_credentials)
                console.print("\n[bold]required credentials[/] (~/.no_human/.env):")
                missing = []
                for key in prof.required_credentials:
                    ok = status.get(key)
                    icon = "[green]✓[/]" if ok else "[red]✗ missing[/]"
                    console.print(f"  {icon} {key}")
                    if not ok:
                        missing.append(key)
                if missing:
                    console.print(f"  [yellow]set {len(missing)} missing key(s) in "
                                  "~/.no_human/.env (chmod 600) before running tasks "
                                  "that need them.[/]")

            prof.save()
            await store.upsert_profile(prof)

            # Visual-proof (ui_evidence) provisioning offer (no-human-67
            # follow-up): a one-action confirm, skipped entirely when
            # ui_evidence is already manually configured — manual config
            # always wins over detection, never re-prompt.
            sug = ui_evidence_suggestion(prof, repo_path)
            if sug:
                console.print(f"\n[bold]{sug['gap']}[/]")

                def _ask(prompt: str) -> bool:
                    try:
                        return click.confirm(prompt, default=False)
                    except (click.Abort, EOFError):
                        return False  # piped/cron stdin => No, never a hang

                try:
                    if await offer_ui_evidence(store, prof, sug, ask=_ask):
                        console.print(f"  [green]✓[/] ui_evidence: "
                                      f"{sug['start_cmd']} → {sug['base_url']}")
                except ProjectYmlPersistError as exc:
                    console.print(f"  [red]✗[/] could not enable ui_evidence: {exc}")

            if prof.proven.get("test_cmd"):
                console.print(f"\n[green]test command proven.[/] confirm to make it "
                              f"usable:\n  [bold]nh onboard {repo} --confirm[/]")
            else:
                console.print("\n[yellow]test command NOT proven[/] — profile is not "
                              "usable until it runs clean. Nothing faked; fix the repo "
                              "or its declarations and re-run.")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Docs generation                                                              #
# --------------------------------------------------------------------------- #


@cli.group("docs")
def docs_group() -> None:
    """Manage auto-generated repo wiki docs."""


@docs_group.command("generate")
@click.argument("repo", type=click.Path(exists=True))
def docs_generate(repo):
    """Generate architecture/modules/conventions wiki for a repo.

    Writes .no_human/wiki/*.md and a pointer block in CLAUDE.md.
    Uses a bounded Agent SDK session (max 12 turns, read-only).

    \b
    Examples:
      nh docs generate ~/git/myrepo
    """
    config, _ = _bootstrap()
    repo_path = str(Path(repo).resolve())
    from ..docs_gen import WikiGenerator
    from ..profile import ProjectProfile

    async def _go():
        backend = ClaudeBackend(
            model=config.primary_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
        )
        gen = WikiGenerator(backend, max_turns=12)
        console.print(f"[bold]generating wiki for[/] {repo_path} …")
        result = await gen.generate(repo_path)
        if result.error:
            console.print(f"[red]error:[/] {result.error}")
            sys.exit(1)
        for f in result.files_written:
            console.print(f"  [green]✓[/] {f}")
        console.print(f"  [green]✓[/] CLAUDE.md (wiki pointer)")
        # Persist wiki_commit to profile.
        profile = ProjectProfile.load(repo_path)
        if profile and result.commit_sha:
            profile.wiki_commit = result.commit_sha
            profile.save()
            console.print(f"  wiki_commit → {result.commit_sha[:8]}")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Team brain (optional, off by default)                                       #
# --------------------------------------------------------------------------- #


class _LazyBrainGroup(click.Group):
    """``nh brain`` without importing ``no_human.brain`` until it is used.

    A plain ``cli.add_command(brain_group)`` would import the whole client on
    every ``nh`` invocation, including ``nh --help`` on a machine that has the
    feature off. Invariant L4 says the package is never imported when the
    feature is off, and ``tests/test_brain_invariants.py`` asserts exactly that
    by importing this module and checking ``sys.modules``.

    It is also the only import site outside prompt assembly, and it fails soft:
    delete ``src/no_human/brain/`` and ``nh brain`` reports that the client is
    not installed. Everything else in the product keeps working, which is the
    other half of L4.
    """

    _NOT_INSTALLED = ("the team-brain client is not installed in this build "
                      "(src/no_human/brain/ is absent)")

    def _delegate(self):
        try:
            from ..brain.cli import brain_group
        except ImportError:
            return None
        return brain_group

    def list_commands(self, ctx):
        delegate = self._delegate()
        return delegate.list_commands(ctx) if delegate else []

    def get_command(self, ctx, cmd_name):
        delegate = self._delegate()
        if delegate is None:
            raise click.UsageError(self._NOT_INSTALLED)
        return delegate.get_command(ctx, cmd_name)


@cli.group("brain", cls=_LazyBrainGroup)
def brain_group() -> None:
    """Team brain: shared, admin-approved rules (off by default)."""


# --------------------------------------------------------------------------- #
# Enterprise CI integration validation (M6)                                          #
# --------------------------------------------------------------------------- #


@cli.group("ci-gate")
def ci_gate_group() -> None:
    """Enterprise CI integration validation (post-PR gate)."""


@ci_gate_group.command("run")
@click.argument("task_id")
@click.option("--poll-interval", type=int, default=None,
              help="Seconds between status polls (default: ci_gate.poll_interval).")
@click.option("--namespace", default=None,
              help="Override the target namespace for this run (default: "
                   "ci_gate.namespace_template). Use when the templated "
                   "namespace is occupied by a stale prior run.")
def ci_gate_run(task_id, poll_interval, namespace):
    """Run the Enterprise CI integration validation for TASK_ID's open PR, now.

    Drives the SAME WakeWatcher rung the server uses — trigger once per PR
    head with the duplicate guards, poll to terminal, post the results
    comment on the PR, and apply the verdict (pass → still awaiting your
    merge; fail → feedback to the coder / escalation). ci_gate.enabled is
    forced ON for this invocation only — running the command is the consent.
    """
    import copy

    config, _ = _bootstrap(require_auth=False)
    cfg = copy.deepcopy(config.data)
    cfg.setdefault("ci_gate", {})["enabled"] = True
    if namespace:
        # A literal namespace formats to itself (no {pr_number} placeholder).
        cfg["ci_gate"]["namespace_template"] = namespace
    interval = poll_interval or int(cfg["ci_gate"].get("poll_interval", 30) or 30)
    from ..blockers import WakeWatcher

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            url = (t.context or {}).get("pr_watch")
            if not url:
                console.print(f"[red]task {t.id[:8]} has no pr_watch URL[/]")
                sys.exit(1)
            watcher = WakeWatcher(
                store, cfg,
                on_event=lambda k, txt: console.print(f"[blue]● {k}[/] {txt}"),
            )
            if watcher._ci_gate_gate is None:
                console.print("[red]Enterprise CI gate wiring failed[/] (see logs)")
                sys.exit(1)
            console.print(f"[bold]Enterprise CI validation[/] for {t.id[:8]} — {url}")
            while True:
                t = await store.get_task(t.id)
                outcome, action = await watcher._ci_gate_step(t, url)
                if outcome is None:
                    console.print("[red]gate step failed[/] (see logs)")
                    sys.exit(1)
                if outcome.action == "skip":
                    console.print(f"[yellow]nothing to do:[/] {outcome.reason}")
                    return
                if action == "ci_gate_passed":
                    console.print(f"[bold green]Enterprise CI integration PASSED[/] — "
                                  f"{outcome.web_url}")
                    return
                if action in ("escalated_ci_gate", "escalated_ci_gate_refused"):
                    console.print(f"[bold red]escalated:[/] {outcome.reason}")
                    sys.exit(1)
                if action == "resumed":
                    console.print(
                        "[bold red]Enterprise CI integration FAILED[/] — failure fed "
                        "back to the coder (task resumed).")
                    sys.exit(1)
                # triggered / waiting / blocked → keep going.
                await asyncio.sleep(interval)

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Blocker handling (PLAN.md Part 22)                                          #
# --------------------------------------------------------------------------- #

_PARKED_STATES = (
    TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
    TaskStatus.PAUSED_QUOTA, TaskStatus.ESCALATED,
)


@cli.command("blocked")
@click.option("--full/--summary", default=False, help="Show the full 6-part report.")
def blocked(full):
    """List parked/escalated tasks with the one question each needs answered."""
    config, _ = _bootstrap(require_auth=False)
    from ..blockers import Blocker, render_report

    async def _go():
        async with Store(config.db_path) as store:
            found = False
            for state in _PARKED_STATES:
                for t in await store.list_tasks(state):
                    found = True
                    b = Blocker.from_dict(t.blocker) if t.blocker else None
                    cat = b.category.value if b else "?"
                    console.print(
                        f"[bold]{t.id[:8]}[/] [yellow]{t.status.value}[/] "
                        f"[magenta]{cat}[/] — {t.title}"
                    )
                    if b and b.question:
                        console.print(f"  [cyan]Q:[/] {b.question}")
                        for i, opt in enumerate(b.options, 1):
                            hint = " [dim](applies a change)[/]" if opt.action else ""
                            console.print(f"     [{i}] {opt.label}{hint}")
                    if b and b.wake_condition:
                        console.print(f"  [dim]wake: {b.wake_condition}[/]")
                    if full and b:
                        console.print(render_report(b, task_title=t.title, task_id=t.id))
                    console.print(
                        f"  [dim]reply:[/] nh reply {t.id[:8]} \"<answer>\""
                    )
            if not found:
                console.print("[green]no blocked tasks[/]")

    asyncio.run(_go())


@cli.command("reply")
@click.argument("task_id")
@click.argument("answer", required=False)
@click.option("--choose", type=int, default=None, metavar="N",
              help="Answer with the blocker's option N (1-based), applying its action.")
@click.option("--run/--no-run", default=True, help="Resume the task now (default).")
def reply(task_id, answer, choose, run):
    """Answer a blocked task's question and resume it from its checkpoint."""
    if (answer is None) == (choose is None):
        raise click.UsageError("give an ANSWER or --choose N, not both")
    # A blank ANSWER is not an answer — same reason as the API's validator: it
    # was stored as a real reply, stranded a plan-gate task in PLANNING with no
    # worker, and would reach the planner as a binding correction saying
    # nothing. Rejected before `_bootstrap` so it costs nothing to be wrong.
    if answer is not None and not answer.strip():
        raise click.UsageError(
            "ANSWER must not be blank — give the text of your answer, or --choose N")
    config, _ = _bootstrap(require_auth=run)
    from ..blockers import (
        ActionError,
        Blocker,
        answer_record,
        apply_action,
        human_event,
        is_plan_approval_action,
        is_terminal_action,
        resume_checkpoint,
        resume_provenance,
    )
    from ..core.bounds import Bounds
    from ..core.budget_floor import check_budget_floor
    from ..core import plan_gate

    async def _go():
        nonlocal answer
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            if t.status not in _PARKED_STATES:
                console.print(
                    f"[yellow]task is {t.status.value}, not blocked[/] — nothing to resume"
                )
                return
            warning = await check_budget_floor(
                store, t, bounds=Bounds.from_config(config.get("bounds")))
            if warning is not None:
                Console(stderr=True).print(f"[yellow]{warning.message()}[/]")
            prior_status = t.status
            prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
            b = Blocker.from_dict(t.blocker) if t.blocker else None
            question = b.question if b else None

            # Picking an option is the only way an action ever runs, and it only
            # runs here, on a human's instruction.
            applied = None
            terminal = False
            approves_plan = False
            if choose is not None:
                if not b or not b.options:
                    console.print("[red]this blocker offers no options[/]")
                    sys.exit(1)
                if not 1 <= choose <= len(b.options):
                    console.print(f"[red]--choose must be between 1 and {len(b.options)}[/]")
                    sys.exit(1)
                option = b.options[choose - 1]
                answer = option.label
                terminal = is_terminal_action(option.action)
                approves_plan = is_plan_approval_action(option.action)
                try:
                    applied = apply_action(
                        t, option.action,
                        bounds=Bounds.from_config(config.get("bounds")))
                except ActionError as exc:
                    console.print(f"[red]cannot apply that option:[/] {exc}")
                    sys.exit(1)
                if applied:
                    console.print(f"[green]applied[/] {applied}")

            record = answer_record(
                question=question, answer=answer,
                attempt_id=(t.blocker or {}).get("attempt_id") or "",
                source="operator:cli",
            )
            record["applied"] = applied
            await store.append_context_list(t.id, "human_replies", record)

            # 2.3 (CodeRabbit learnings): if the reply states a reusable
            # preference/rule, propose it to the HUMAN-CONFIRMED learning queue
            # (confirmed=False — never auto-active) so future reviews apply it.
            if answer:
                from ..history.analyzer import mine_reply
                mined = mine_reply(answer)
                if mined:
                    category, desc = mined
                    # `source` is the queue-VISIBILITY contract, not a place to
                    # name the producer — this call passed source="reply" and
                    # so wrote rows `pending()` selects past. Two real ones
                    # (2026-07-26/27, from the operator's own review replies)
                    # were sitting unqueueable in the live database when this
                    # was found. The provenance goes in `origin`, and
                    # `Store.add_memory` now refuses the old shape outright.
                    from ..learning import ORIGIN_REPLY
                    proposed = await store.add_memory(
                        mem_type=category,
                        title=f"{desc} (from a review reply)"[:120],
                        content=answer, source="proposed", confirmed=False,
                        origin=ORIGIN_REPLY,
                        project=t.repo_path,
                        tags=["reply", category, "user_correction"],
                        dedupe_key=f"reply:{answer[:80]}",
                    )
                    if proposed:
                        console.print(f"[dim]captured a learning from your reply "
                                      f"({category}) — confirm with `nh learnings`[/]")
            # A terminal option (SCRUM-22: "stop — keep the work parked as-is")
            # means exactly that: record the answer, apply nothing else, and
            # LEAVE the task in its parked state. Resuming here is what
            # silently inverted the human's explicit stop.
            if terminal:
                # Review 2026-07-25: stamp the stop or the wake watcher's
                # sweep undoes it (max_park re-escalation, wake_condition
                # resume) — the printed promise below was false without this.
                blocker_data = dict(t.blocker or {})
                blocker_data["human_stopped"] = True
                t.blocker = blocker_data
                t.wake_check_at = None
                await store.update_task_columns(t)
                console.print(f"[yellow]kept parked[/] {t.id[:8]} — "
                              "work preserved as-is; nothing will resume it")
                return
            # Continue from the [WIP-BLOCKED] checkpoint instead of re-doing the
            # work from base. The blocker promised "Resume with: nh reply …".
            checkpoint = resume_checkpoint(t.blocker)
            # Unconditional — see `WakeWatcher._resume`.
            patch = {"resume_from": resume_provenance(checkpoint, "human")}
            # GAP 1 plan-approval gate: only the approve OPTION approves; free
            # text is a correction and resumes into PLANNING to be re-planned.
            # "At the gate" is the LIVE blocker carrying the approve option
            # (`plan_gate.at_gate`), never a context flag — see api/app.py.
            # Inert off the gate — the resume target stays IMPLEMENTING.
            resume_to = plan_gate.resume_status(t, approve=approves_plan)
            if plan_gate.at_gate(t):
                patch[plan_gate.CONTEXT_KEY] = plan_gate.reply_patch(
                    t, approve=approves_plan, answer=answer or "")
            t.context = await store.merge_context(t.id, patch)
            t.wake_check_at = None
            await store.update_task_columns(t)
            # Answering a blocker withdraws any pending stop (a task paused by
            # `nh task pause` is resumed by answering it).
            await store.clear_cancel_request(t.id)
            # Resume into the working loop from the [WIP-BLOCKED] checkpoint.
            await store.set_status(
                t, resume_to, validate=False,
                event=human_event(
                    "reply", prior_status=prior_status, prior_blocker=prior_blocker,
                    actor="cli"),
            )
            console.print(f"[green]resumed[/] {t.id[:8]} with your answer")
            if not run:
                console.print(f"run it with:  [bold]nh watch {t.id[:8]}[/]")
                return
            if _server_owns_worker(config):
                # The task is IMPLEMENTING (or PLANNING, for a plan-approval
                # correction), both of which the server's scheduler claims.
                # Running it here too would put two orchestrators on one checkout.
                console.print(
                    "[cyan]the running server picked it up[/] — "
                    f"watch it with: [bold]nh watch {t.id[:8]}[/]"
                )
                return
            console.rule(f"[bold]resuming {t.id[:8]}")
            async with EventPersister(store, t.id) as persister:
                orch = _build_orchestrator(
                    config, store,
                    event_sink=_persisting(persister, t.id, render_event), task=t)
                outcome = await orch.run_task(t)
            console.rule(f"[bold]{outcome.status.value}")
            if outcome.pr_url:
                console.print(f"[bold green]PR:[/] {outcome.pr_url}")
            console.print(outcome.detail)

    asyncio.run(_go())


@cli.command("wake")
@click.option("--loop", is_flag=True, help="Poll continuously at wake_poll_interval.")
def wake(loop):
    """Run the wake-condition watcher: resume parked tasks whose condition fired,
    escalate tasks parked past max_park_duration (Part 22.7).

    Time-based (`after:`, `quota_refreshed`) and timeout conditions resolve out of
    the box. `pr_merged:` / `ci_green_on:` resolve via gh/glab when available.
    Run once (cron-friendly) or with --loop.
    """
    config, _ = _bootstrap(require_auth=False)
    from ..blockers import WakeWatcher, parse_duration
    from ..vcs.pr_watcher import (
        branch_landed_commit, check_pr_comments, default_ci_annotations,
        default_ci_log_excerpt, default_pr_checks, default_pr_merged,
        default_pr_mergeable, default_pr_state,
    )

    async def _tick_once(store):
        watcher = WakeWatcher(
            store, config.data,
            pr_merged=default_pr_merged, pr_comment=check_pr_comments,
            pr_state=default_pr_state, pr_checks=default_pr_checks,
            pr_mergeable=default_pr_mergeable,
            ci_log=default_ci_log_excerpt,
            ci_annotations=default_ci_annotations,
            pr_shipped=branch_landed_commit,
            on_event=lambda kind, text: console.print(f"[blue]● {kind}[/] {text}"),
        )
        actions = await watcher.tick()
        if not actions:
            console.print("[dim]no parked tasks ready[/]")
        return actions

    async def _go():
        async with Store(config.db_path) as store:
            if not loop:
                await _tick_once(store)
                return
            interval = parse_duration(
                str(config.data.get("blockers", {}).get("wake_poll_interval", "10m")))
            secs = int(interval.total_seconds()) if interval else 600
            console.print(f"[dim]watching parked tasks every {secs}s (ctrl-c to stop)[/]")
            import asyncio as _a
            while True:
                await _tick_once(store)
                await _a.sleep(secs)

    asyncio.run(_go())


async def _jira_poll_loop(poller, stop, poll_interval: int) -> None:
    """Tick the Jira poller every ``poll_interval`` seconds until ``stop`` is set.
    Mirrors Scheduler.run_forever's stop-aware wait so ctrl-c stays responsive."""
    while not stop.is_set():
        try:
            await poller.tick()
        except Exception as exc:  # noqa: BLE001 — never kill serve on a Jira hiccup
            console.print(f"[red]Jira poll error[/] {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass


async def _linear_poll_loop(poller, stop, poll_interval: int) -> None:
    """Tick the Linear poller every ``poll_interval`` seconds until ``stop`` is
    set. Deliberately a sibling of ``_jira_poll_loop`` rather than a shared
    helper, matching the precedent already set for the Jira block in `start`:
    each tracker keeps its own patchable seam and its own log label."""
    while not stop.is_set():
        try:
            await poller.tick()
        except Exception as exc:  # noqa: BLE001 — never kill serve on a Linear hiccup
            console.print(f"[red]Linear poll error[/] {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass


async def _monday_poll_loop(poller, stop, poll_interval: int) -> None:
    """Tick the monday poller every ``poll_interval`` seconds until ``stop`` is
    set. A sibling of ``_jira_poll_loop``/``_linear_poll_loop`` rather than a
    shared helper, matching the precedent those two already set: each tracker
    keeps its own patchable seam and its own log label."""
    while not stop.is_set():
        try:
            await poller.tick()
        except Exception as exc:  # noqa: BLE001 — never kill serve on a monday hiccup
            console.print(f"[red]monday poll error[/] {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass


@cli.command("serve")
@click.option("--max-workers", type=int, default=None,
              help="Run the pool with this many workers for this invocation, "
                   "even if concurrency.enabled is false in config (config on "
                   "disk is left untouched). Refused above 1 worker when "
                   "isolation.enabled is false.")
@click.option("--until-empty", is_flag=True, default=False,
              help="Work the queue, then exit instead of running forever "
                   "(cron/CI). Exits 0 when nothing is claimable and nothing "
                   "is in flight, 1 if any task this run dispatched ended "
                   "FAILED or the drain was cut short by a signal, 2 if a "
                   "mid-run row exists that no worker in this process owns "
                   "(a crash orphan, or one owned elsewhere) and is not yet "
                   "claimable — named in the output with the seconds until "
                   "it becomes claimable. Parked tasks (blocked/awaiting-"
                   "input/escalated/paused-quota) are not claimable: they "
                   "end the drain and do not fail it.")
@click.option("--no-harvest", is_flag=True, default=False,
              help="Skip the scheduled learning-harvest pass for this "
                   "invocation, even if harvest.enabled is true in config "
                   "(config on disk is left untouched). The pass only ever "
                   "PROPOSES (nothing auto-applies) — see `nh learnings "
                   "--harvest` to run it by hand instead.")
def serve(max_workers, until_empty, no_harvest):
    """Run the concurrent scheduler daemon (Phase 7): drain pending + resumed
    tasks into a bounded worker pool, each task in its own git worktree, running
    the wake-watcher in the same loop. Ctrl-C to stop (drains in-flight tasks).

    Requires concurrency.enabled in config, or pass --max-workers N to run the
    pool for this run only. Worktree isolation (isolation.enabled) is on by
    default for every task and is mandatory for a pool wider than one worker.

    Add --until-empty to make it a batch job: same loop, same graceful
    shutdown, but the queue going empty sets the stop event a signal would.

    Refuses to start beside a running `nh start` / `nh serve` over the same
    checkout — the two are mutually exclusive, since either one owns the
    worker pool.
    """
    config, _ = _bootstrap()
    _assert_backend_usable()
    _warn_if_editable_install_dangles()
    from ..blockers import WakeWatcher, parse_duration
    from ..core.scheduler import (PoolLeaseLost, PoolLeaseUnreadable, Scheduler,
                                  SiblingSchedulerRunning, clamp_pool_width,
                                  resolve_serve_pool)

    conc = config.data.setdefault("concurrency", {})
    workers, enabled, error = resolve_serve_pool(config.data, cli_workers=max_workers)
    if error:
        console.print(f"[yellow]{error}[/]")
        sys.exit(1)
    # `resolve_serve_pool` has already applied the machine ceiling; the reason
    # is re-derived here from the width that was ASKED for, because a clamp is
    # a downgrade and `error` means "do not serve". Silently serving a narrower
    # pool than requested is the failure this prints away.
    _asked = max_workers if max_workers is not None else int(
        conc.get("max_workers", 2) or 2)
    _, _clamp_reason = clamp_pool_width(_asked)
    if _clamp_reason:
        console.print(f"[yellow]⚠ {_clamp_reason}[/]")
    if max_workers is not None:
        # An explicit flag enables the pool + worktree isolation for this
        # invocation only — the config default on disk is left untouched.
        conc["enabled"] = True
        conc["max_workers"] = workers

    # `nh serve` binds no socket, so the pid lock is the ONLY thing a CLI
    # runner (`nh task new --run`, `nh reply --run`, `nh watch`) can see. It is
    # the same lock `nh start` takes: the two are mutually exclusive by design,
    # since either one owns the worker pool over a single checkout.
    if not _acquire_pid_lock():
        console.print(
            "[red]another no_human instance is already running[/]\n"
            "Kill it first, or remove the stale lock:\n"
            "  [bold]rm ~/.no_human/nh.pid[/]"
        )
        sys.exit(1)

    interval = parse_duration(str(conc.get("poll_interval", "10s")))
    secs = int(interval.total_seconds()) if interval else 10

    async def _go() -> int:
        async with Store(config.db_path) as store:
            from ..vcs.pr_watcher import (
        branch_landed_commit, check_pr_comments, default_ci_annotations,
        default_ci_log_excerpt, default_pr_checks, default_pr_merged,
        default_pr_mergeable, default_pr_state,
    )
            watcher = WakeWatcher(
                store, config.data,
                pr_merged=default_pr_merged, pr_comment=check_pr_comments,
            pr_state=default_pr_state, pr_checks=default_pr_checks,
            pr_mergeable=default_pr_mergeable,
            ci_log=default_ci_log_excerpt,
            ci_annotations=default_ci_annotations,
            pr_shipped=branch_landed_commit,
                on_event=lambda k, t: console.print(f"[blue]● {k}[/] {t}"))
            # PR-E: periodic re-analysis job (EVOLUTION_PLAN Phase 9).
            reanalysis = None
            ra_cfg = config.data.get("reanalysis", {})
            if ra_cfg.get("enabled", True):
                from ..core.scheduler import ReanalysisJob
                reanalysis = ReanalysisJob(
                    store,
                    interval_seconds=float(ra_cfg.get("interval_seconds", 86400)),
                    days=int(ra_cfg.get("days", 30)),
                    max_proposals_per_run=int(ra_cfg.get("max_proposals", 20)),
                )
            # M-A: background repo-wiki refresh (docs_gen). Opt-in via
            # docs.auto_refresh — off by default so serve incurs no unattended
            # backend cost. Uses a read-only recon backend (write tools blocked).
            wiki_refresh = None
            docs_cfg = config.data.get("docs", {})
            if docs_cfg.get("auto_refresh", False):
                from ..core.scheduler import WikiRefreshJob
                recon_backend = ClaudeBackend(
                    model=config.primary_model,
                    forbidden_paths=config["safety"]["forbidden_paths"],
                    never_push_to=config["git"]["never_push_to"],
                    readonly=True,
                )
                wiki_refresh = WikiRefreshJob(
                    store, recon_backend,
                    interval_seconds=float(docs_cfg.get("refresh_interval_seconds", 3600)),
                    max_turns=int(docs_cfg.get("max_turns", 12)),
                )
                console.print("[green]wiki auto-refresh[/] enabled "
                              f"(every {docs_cfg.get('refresh_interval_seconds', 3600)}s)")
            # Learning harvest: supervisor corrections + escalations +
            # reviewer FAIL findings + tamper trips, clustered and proposed
            # on a cadence in this same loop. `--no-harvest` and
            # `harvest.enabled: false` are both opt-outs for this run; the
            # pass never calls a model and never does anything beyond
            # propose (see `HarvestJob`'s docstring in core/scheduler.py).
            harvest = None
            harvest_cfg = config.data.get("harvest", {})
            if harvest_cfg.get("enabled", True) and not no_harvest:
                from ..core.scheduler import HarvestJob
                # D3 (2026-08-31 operator directive): `learning.auto_manage`/
                # `learning.auto_activate_daily_cap` threaded through here too
                # — without this, `nh serve`'s HarvestJob would silently keep
                # the constructor defaults (auto-management ON) regardless of
                # what an operator set in `config.yaml`, and the kill switch
                # would be inert on this, the CLI-driven scheduling path.
                learning_cfg = config.data.get("learning", {})
                harvest = HarvestJob(
                    store,
                    interval_seconds=float(harvest_cfg.get("interval_seconds", 43200)),
                    auto_manage=bool(learning_cfg.get("auto_manage", True)),
                    auto_activate_daily_cap=int(
                        learning_cfg.get("auto_activate_daily_cap", 10)),
                )
                console.print("[green]learning harvest[/] enabled "
                              f"(every {harvest_cfg.get('interval_seconds', 43200)}s)")
            sched = Scheduler(
                store, lambda task=None: _build_orchestrator(config, store, event_sink=render_event, task=task),
                max_workers=workers, wake_watcher=watcher,
                on_event=lambda k, t: console.print(f"[magenta]▸ {k}[/] {t}"),
                reanalysis_job=reanalysis,
                wiki_refresh_job=wiki_refresh,
                harvest_job=harvest,
                config=config.data)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            import signal
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:  # pragma: no cover — non-unix
                    pass
            console.print(f"[green]serving[/] pool={workers} poll={secs}s "
                          + ("(until the queue is empty)" if until_empty
                             else "(ctrl-c to stop)"))

            # Jira intake: poll the operator's JQL into tasks (+ opt-in status
            # write-back) on its own cadence, in the same loop. Opt-in via
            # integrations.jira.enabled.
            coros = [sched.run_forever(stop=stop, poll_interval=secs,
                                       until_empty=until_empty)]
            jira_cfg = (config.data.get("integrations") or {}).get("jira") or {}
            if jira_cfg.get("enabled"):
                from ..config import load_env_var
                from ..intake.jira import JiraAdapter
                from ..intake.jira_poll import JiraPoller
                load_env_var("JIRA_API_TOKEN")  # from ~/.no_human/.env into the process env
                jira_secs = max(60, int((parse_duration(str(jira_cfg.get("poll_interval", "5m")))
                                         or parse_duration("5m")).total_seconds()))
                poller = JiraPoller(
                    JiraAdapter(config.data), store, config=config.data,
                    on_event=lambda k, t: console.print(f"[cyan]◆ {k}[/] {t}"))
                console.print(f"[green]Jira intake[/] project={jira_cfg.get('project_key') or '?'} "
                              f"poll={jira_secs}s")
                coros.append(_jira_poll_loop(poller, stop, jira_secs))

            # Linear intake: same role as the Jira block above (a polled issue
            # source, not an `nh task add` argument). Opt-in via
            # integrations.linear.enabled.
            linear_cfg = (config.data.get("integrations") or {}).get("linear") or {}
            if linear_cfg.get("enabled"):
                from ..config import load_env_var
                from ..intake.linear import LinearAdapter
                from ..intake.linear_poll import LinearPoller
                load_env_var("LINEAR_API_KEY")  # from ~/.no_human/.env into the process env
                linear_secs = max(60, int((parse_duration(str(linear_cfg.get("poll_interval", "5m")))
                                           or parse_duration("5m")).total_seconds()))
                linear_poller = LinearPoller(
                    LinearAdapter(config.data), store, config=config.data,
                    on_event=lambda k, t: console.print(f"[cyan]◆ {k}[/] {t}"))
                console.print(f"[green]Linear intake[/] team={linear_cfg.get('team_key') or '?'} "
                              f"poll={linear_secs}s")
                coros.append(_linear_poll_loop(linear_poller, stop, linear_secs))

            # monday.com intake: same role again. The console line names the
            # BOARD and the STATUS COLUMN rather than a team key, because those
            # two are what monday intake actually depends on — it has no typed
            # workflow state, so an unset status column means it cannot run.
            monday_cfg = (config.data.get("integrations") or {}).get("monday") or {}
            if monday_cfg.get("enabled"):
                from ..config import load_env_var
                from ..intake.monday import MondayAdapter
                from ..intake.monday_poll import MondayPoller
                load_env_var("MONDAY_API_TOKEN")  # from ~/.no_human/.env into the process env
                monday_secs = max(60, int((parse_duration(str(monday_cfg.get("poll_interval", "5m")))
                                           or parse_duration("5m")).total_seconds()))
                monday_poller = MondayPoller(
                    MondayAdapter(config.data), store, config=config.data,
                    on_event=lambda k, t: console.print(f"[cyan]◆ {k}[/] {t}"))
                console.print(
                    f"[green]monday intake[/] board={monday_cfg.get('board_id') or '?'} "
                    f"status_column={monday_cfg.get('status_column') or '?'} "
                    f"poll={monday_secs}s")
                coros.append(_monday_poll_loop(monday_poller, stop, monday_secs))

            # Slack intake: Socket-Mode worker with the @mention handler
            # attached (below), opt-in via integrations.slack.intake. Disabled
            # by default -> not imported, not constructed, zero new behavior.
            # A setup/connect failure is caught so a misconfigured Slack
            # integration never breaks `serve`.
            slack_cfg = (config.data.get("integrations") or {}).get("slack") or {}
            slack_worker = None
            if slack_cfg.get("intake"):
                try:
                    from ..integrations.slack import (
                        AppMentionHandler, SlackWorker)
                    slack_worker = SlackWorker(
                        config.data,
                        on_event=lambda k, t: console.print(f"[cyan]◆ {k}[/] {t}"))
                    await asyncio.to_thread(slack_worker.start)
                    # Attach the @mention handler so a mention actually creates a
                    # task. register() captures THIS running loop; _dispatch, on
                    # the SocketMode callback thread, bridges each event back to
                    # it. The worker's own reply posts the handler's in-thread
                    # answers. Without this call the socket connected but every
                    # @mention was acked and DROPPED — the gap this closes.
                    slack_handler = AppMentionHandler(
                        store, slack_worker.reply, config=slack_cfg,
                        on_event=lambda k, t: console.print(f"[cyan]◆ {k}[/] {t}"))
                    slack_handler.register(slack_worker)
                    console.print(
                        "[green]Slack intake[/] socket connected; @mention me "
                        "with repo:<name> to create a task.")
                except Exception as exc:  # noqa: BLE001 — optional integration, never break `serve`
                    console.print(f"[yellow]Slack intake failed to start[/] {exc}")
                    slack_worker = None

            try:
                await asyncio.gather(*coros)
            finally:
                if slack_worker is not None:
                    await asyncio.to_thread(slack_worker.stop)
            # "drained" is a CLAIM about the queue, so it is made only where
            # it is true — the exit-0 path below. Saying it here printed
            # "drained; stopped" immediately above "not drained task ...",
            # which re-created, in the log, exactly the false signal this
            # command was fixed to stop giving (task 920228c9, review of #624).
            console.print("[dim]stopped[/]")

            if not until_empty:
                return 0
            # The batch caller's only channel is the exit code, so it says
            # what happened rather than that the process ended. Failures
            # first: a run that both failed a task and was cut short is a
            # failed run.
            err = Console(stderr=True)
            failed = await sched.failed_dispatched()
            if failed:
                err.print(f"[red]{len(failed)} task(s) FAILED[/] "
                          + ", ".join(t[:8] for t in failed))
                return 1
            # Checked BEFORE the generic `queue_is_drained` signal check
            # below: `queue_is_drained` is now also False for a stranded row
            # (see scheduler.py), so if this branch ran second the operator
            # would get "stopped before the queue drained (signalled)" — a
            # false claim, since nothing signalled this run.
            stranded = list(getattr(sched, "drain_blocked_by", None)
                             or await sched.unclaimable_orphans())
            if stranded:
                for row in stranded:
                    err.print(
                        f"[red]not drained[/] task {row['task_id'][:8]} is "
                        f"{row['status']} with no worker attached in this "
                        f"process — claimable in "
                        f"{row['seconds_until_claimable']:.0f}s")
                return 2
            if not await sched.queue_is_drained():
                err.print("[red]stopped before the queue drained[/] "
                          "(signalled) — work is still claimable")
                return 1
            console.print("[dim]drained[/]")
            return 0

    try:
        rc = asyncio.run(_go()) or 0
    except (SiblingSchedulerRunning, PoolLeaseLost, PoolLeaseUnreadable) as exc:
        # `run_forever`'s very first call, unguarded on purpose — this is
        # the operator-visible refusal that call is FOR. See scheduler.py's
        # `SiblingSchedulerRunning` docstring for the incident (6408aba0)
        # this prevents. `PoolLeaseLost`/`PoolLeaseUnreadable` are the same
        # refusal for a claim that could not be read or could not be proven
        # to land (fail-closed, not fail-open) — same exit code, same
        # operator-visible reason.
        console.print(f"[red]{exc}[/]")
        sys.exit(1)
    finally:
        _release_pid_lock()
    sys.exit(rc)


@cli.command("status")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit lane bucket counts as a JSON object to stdout.")
def status(as_json):
    """Show task counts by lane: queued (pending), running (in-flight stages),
    parked, and terminal. A quick portfolio read across all projects."""
    if as_json:
        mark_machine_output()
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        # Lazy: keeps `commands.py`'s import graph from pulling in the API
        # layer at CLI-startup time, matching the existing `from ..api.app
        # import app as _app` precedent elsewhere in this file. This is the
        # SAME predicate `api/models.py:489` (TaskSummaryOut) and the drawer's
        # TaskOut.cancelled field read, so `nh status`, the CLI Failed lane,
        # and the API can never disagree about which failed tasks were
        # operator cancels.
        from ..api.models import _operator_cancelled, merge_ready_for
        async with Store(config.db_path) as store:
            tasks = await store.list_tasks()
            waiting_ids = await store.tasks_waiting_for_slot()
            # The denominator is the RUNNING pool when one is reachable —
            # `nh start --workers N` overrides the config without writing it,
            # so the config number is a guess about a process this command can
            # simply ask. When it can't ask — including under `nh serve`, which
            # binds no socket at all (see `_running_pool_stats`'s KNOWN GAP) —
            # it says which number it is printing rather than implying it
            # observed one. Same discipline for the NUMERATOR: a worker-owned
            # status row (IMPLEMENTING among them) is not evidence of a
            # running worker — it can be stranded, claimable and waiting,
            # after a restart. The live `workers_busy` count is what's honest;
            # any row in excess of it is reclassified into `queued`, which is
            # where `/api/queue/health`'s own `queue_depth` already counts an
            # unclaimed IMPLEMENTING row.
            #
            # Computed ONCE, before the `--json` branch (review finding F1):
            # the JSON branch used to return its own bucket counts BEFORE this
            # pause/reachability check ran, so `nh status` and
            # `nh status --json` could disagree about `waiting` for the exact
            # same DB state at the exact same moment (human-readable said 0
            # while paused; `--json` still said 1). There is no DB-persisted
            # substitute for this signal — `_quota_cooldown_until` lives only
            # in the running Scheduler's memory — so both branches now share
            # one HTTP probe and one bucket computation. This knowingly
            # supersedes PLAN.md's OOS note that `--json` must avoid an HTTP
            # call and stay byte-identical; the no-users rule licenses the
            # break, and the alternative (reverting AC3's already-landed,
            # already-tested human-readable fix) would mean weakening a
            # passing test, which is not allowed.
            probe = _probe_pool(config)
            stats = probe.stats
            pause = stats[2] if stats is not None else None
            # While the pool is paused nothing is competing for a slot, so
            # calling a recorded wait a *live* slot wait is the same class of
            # lie the module docstring already names — the waiter falls out
            # of `waiting` and back into the ordinary working/queued split
            # below, same as any other unclaimed row. `reachable=False` when
            # the pool couldn't be asked at all (review finding F2): otherwise
            # `waits_are_live(None)` reads "no pause reported" and "couldn't
            # find out" as the same thing, so an unreachable pool's stale wait
            # was being printed with the same confidence as a live one. Fail
            # closed instead — an unknown pool state is not a live wait.
            if not slot_wait.waits_are_live(pause, reachable=stats is not None):
                waiting_ids = set()
            buckets = status_buckets(tasks, waiting_ids)
            # The cancelled split (#551) rides beside the bucket counts: a
            # cancelled task ends FAILED but is not a capability failure.
            cancelled_failed = sum(
                1 for t in tasks
                if t.status == TaskStatus.FAILED and _operator_cancelled(t))
            # The intake spend no task owns (interactive grill rounds run
            # before a task exists; pre-attempt intake on tasks that never
            # reached an attempt). Read BEFORE the --json return, not after:
            # the GUI wizard is the biggest producer of these rows and a
            # board-only operator reads this command through --json, so
            # computing it only on the human-readable branch would hide the
            # spend from exactly the operator who generated it.
            resid = await store.unattributed_usage_totals()
            # Board's MERGE-READY chip, counted here too: only tasks actually
            # sitting in Review PR (awaiting_approval) with a ready verdict
            # for their CURRENT head — the same `merge_ready_for` the board
            # card (api/models.py) reads, so the two can never disagree.
            by_task = await store.attempts_by_task()
            merge_ready_n = sum(
                1 for t in tasks
                if t.status == TaskStatus.AWAITING_APPROVAL
                and merge_ready_for(t, by_task.get(t.id) or []) is True
            )
            if as_json:
                # Nested under its own key so the existing bucket keys keep
                # their shape — a consumer that ignores it sees no change, and
                # it is NOT summed into any per-task figure. `buckets` here is
                # the SAME pause/reachability-aware computation the
                # human-readable branch below uses (see F1 comment above) —
                # no second, disagreeing bucket count.
                click.echo(json.dumps({**buckets, "unattributed_usage": resid}))
                return
            if stats is None:
                mw = config.data.get("concurrency", {}).get("max_workers", 1)
                mw_note = _pool_note(probe.outcome, probe.http_status)
                working_n, queued_n = buckets["working"], buckets["queued"]
            else:
                busy, mw, pause = stats
                mw_note = ""
                if busy is None:
                    working_n, queued_n = buckets["working"], buckets["queued"]
                else:
                    working_n = busy
                    queued_n = buckets["queued"] + max(0, buckets["working"] - busy)
            # Human-readable only — an operator cancel is not a capability
            # failure (same split the board's Outcomes table and the CLI
            # Failed lane make). The --json bucket keeps summing both: it is a
            # documented consumer contract this line does not touch.
            real_failed = buckets["failed"] - cancelled_failed
            failed_display = (f"{real_failed} (+{cancelled_failed} cancelled)"
                               if cancelled_failed else f"{real_failed}")
            console.print(
                f"[yellow]needs you[/] {buckets['needs you']}  "
                f"[dim]queued[/] {queued_n}  "
                f"[bold]working[/] {working_n}/{mw}{mw_note}  "
                f"[blue]waiting[/] {buckets['waiting']}  "
                f"[red]failed[/] {failed_display}  "
                f"[green]done[/] {buckets['done']}")
            console.print(f"[dim]merge-ready:[/] {merge_ready_n}")
            # Same fields the board header reads from `/api/queue/health` —
            # a quota-paused pool prints WHY nothing is moving instead of a
            # bare `working 0/N` next to a ETA computed as if work were
            # flowing (2026-08-20 evidence: "not stuck, 0 busy, ETA 210 min").
            if pause and pause.get("reason") == "infra":
                until = _local_hhmm(pause.get("until"))
                console.print(
                    f"[magenta]paused[/] — SDK/auth failures, resumes {until}")
            elif pause and pause.get("reason") == "quota":
                who = f" ({pause['profile']} profile)" if pause.get("profile") else ""
                until = _local_hhmm(pause.get("until"))
                console.print(
                    f"[magenta]paused[/] — quota cooldown{who}, resumes {until}")
            # Printed only when there IS a residual (whole-ledger total, same
            # gate as before), so the line appears exactly when it has
            # something to say. Within it, "no task owns it" is scoped to the
            # genuinely ownerless half only — the attributed half (site
            # prefix `orphaned_*`) is recorded against a task, just not yet
            # folded into that task's attempt rows, so it gets its own
            # clause instead of being called ownerless.
            if resid["total"]:
                owned = await store.unattributed_usage_totals(attributed=True)
                # Derived by subtraction from the single whole-ledger query
                # above, rather than a second `attributed=False` query, so
                # the two halves are guaranteed to sum to `resid` instead of
                # being able to disagree if the ledger changes between calls.
                ownerless_total = resid["total"] - owned["total"]
                ownerless_calls = resid["calls"] - owned["calls"]
                parts = []
                if ownerless_total:
                    parts.append(
                        f"unattributed intake spend: {ownerless_total:,} tokens "
                        f"over {ownerless_calls} call(s) — no task owns it")
                if owned["total"]:
                    clause = (
                        f"{owned['total']:,} tokens over {owned['calls']} "
                        f"call(s) recorded to tasks but not in their attempt "
                        f"rows")
                    parts.append(f"plus {clause}" if parts else clause)
                console.print(f"[dim]{'; '.join(parts)}[/]")

    asyncio.run(_go())


@cli.command("autonomy")
@click.option("--days", default=None, type=int,
              help="Only include tasks created in the last N days.")
def autonomy(days):
    """Autonomy telemetry (megaplan P0): how often a human is pulled in
    mid-flight vs. tasks reaching a SETTLED state. The North Star is a
    touchpoint rate near zero — the only human steps are starting the site
    and reviewing/merging the final PR.

    `PR-reached` does NOT mean a pull request exists. It counts tasks whose
    status is AWAITING_APPROVAL or DONE, and two of the three orchestrator
    paths to AWAITING_APPROVAL open no PR at all — the "already satisfied, no
    code change needed" path and the code-review path, which ends with draft
    comments awaiting approval and none posted. This docstring previously said
    "reaching a reviewable PR", which was wrong in both halves. For what the
    forge actually reports about a PR — opened, merged, closed unmerged — use
    `nh pr-outcomes`, which is a separate instrument over a separate
    population and abstains rather than guessing."""
    config, _ = _bootstrap(require_auth=False)
    from ..core.autonomy import compute_autonomy_metrics

    def _pct(x: float | None) -> str:
        return f"{x:.0%}" if x is not None else "n/a"

    async def _go():
        async with Store(config.db_path) as store:
            rep = await compute_autonomy_metrics(store, days=days)
            window = f"last {days}d" if days else "all time"
            console.rule(f"[bold]autonomy — {window}")
            if rep.settled_tasks == 0:
                console.print("[dim]no settled tasks yet[/]")
                return
            console.print(
                f"[green]PR-reached[/] {rep.pr_reached}/{rep.settled_tasks} "
                f"({_pct(rep.pr_reached_rate)})   "
                f"[yellow]mid-flight touchpoints[/] {rep.touchpoint_tasks}/"
                f"{rep.settled_tasks} ({_pct(rep.touchpoint_rate)})")
            # The caveat rides WITH the number, not in --help. A reader who
            # sees "PR-reached 41/44" and no qualifier will read it as 44
            # pull requests; two of the three paths to that state open none.
            console.print(
                "[dim]PR-reached counts settled tasks (awaiting-approval or "
                "done), NOT pull requests that exist — see `nh pr-outcomes` "
                "for what the forge reports.[/]")
            if rep.turn_exhaustion_empty:
                console.print(
                    f"[red]turn-exhaustion empty-diff attempts[/] "
                    f"{rep.turn_exhaustion_empty}")
            if rep.by_status:
                table = Table(title="tasks by status")
                table.add_column("status")
                table.add_column("count", justify="right")
                for status, n in sorted(rep.by_status.items(),
                                        key=lambda kv: -kv[1]):
                    table.add_row(status, str(n))
                console.print(table)
            if rep.blocker_categories:
                table = Table(title="blocker categories (pull a human in)")
                table.add_column("category")
                table.add_column("count", justify="right")
                for cat, n in sorted(rep.blocker_categories.items(),
                                     key=lambda kv: -kv[1]):
                    table.add_row(cat, str(n))
                console.print(table)

    asyncio.run(_go())


@cli.group("pr-outcomes")
def pr_outcomes_group():
    """What actually happened to the PRs (migration 0010).

    `nh autonomy` counts tasks that reached a reviewable state. It never asks
    whether the PR merged. These two commands do.
    """


@pr_outcomes_group.command("show")
@click.option("--days", default=None, type=int,
              help="Only include tasks created in the last N days.")
def pr_outcomes_show(days):
    """Delivered vs merged vs unknown, as separate figures.

    Reads only what has been recorded — it never contacts the forge, so it is
    safe and instant offline. Run `nh pr-outcomes refresh` to update the
    recorded outcomes first.
    """
    config, _ = _bootstrap(require_auth=False)
    from ..core.autonomy import compute_pr_outcome_metrics, render_pr_outcome_lines

    async def _go():
        async with Store(config.db_path) as store:
            rep = await compute_pr_outcome_metrics(store, days=days)
            console.rule(f"[bold]pr outcomes — {f'last {days}d' if days else 'all time'}")
            for line in render_pr_outcome_lines(rep):
                console.print(escape(line), markup=False)

    asyncio.run(_go())


@pr_outcomes_group.command("refresh")
@click.option("--limit", default=200, type=int, show_default=True,
              help="Maximum rows to re-poll in one invocation.")
def pr_outcomes_refresh(limit):
    """Re-poll every UNSETTLED recorded PR against the forge.

    THIS IS THE MANUAL REFRESH TRIGGER. A PR merges hours or days after the run
    that opened it ended, so the outcome written at PR-open time is a snapshot
    of "open" and nothing else. Two things update it:

    \b
      * the wake watcher, automatically, while `nh serve` is running and the
        task is still AWAITING_APPROVAL;
      * this command, for everything the watcher cannot see — tasks that have
        already gone terminal (DONE/ESCALATED/FAILED), and rows recorded while
        the machine was offline.

    If NEITHER ever runs, recorded outcomes simply stay as they were: mostly
    `open` or `unknown`. Nothing decays into a success — `unknown` is never
    counted as merged — so a stale table under-reports merges and never
    over-reports them.

    Needs `gh` and network. Without them every poll returns "unknown", which is
    recorded as unknown and leaves any already-known outcome untouched.
    """
    config, _ = _bootstrap(require_auth=False)
    from ..vcs.pr_outcome import UNKNOWN, refresh_outcomes
    from ..vcs.pr_watcher import default_pr_checks, default_pr_state

    async def _go():
        async with Store(config.db_path) as store:
            async def _shipped(pr_url: str, task_id: str):
                # Resolve the CLOSED ambiguity the only way that is honest: ask
                # git about the task's OWN repo and branch, and return None
                # (not False) whenever that cannot be done. See
                # `pr_outcome.probe_shipped`.
                from ..vcs.pr_outcome import probe_shipped
                task = await store.get_task(task_id)
                if task is None:
                    return None
                ctx = task.context or {}
                return await probe_shipped(task.repo_path, ctx.get("pr_branch"),
                                           ctx.get("base_branch") or "main")

            tally = await refresh_outcomes(
                store, pr_state=default_pr_state, pr_checks=default_pr_checks,
                shipped_probe=_shipped, limit=limit)
            total = sum(tally.values())
            console.print(f"[green]re-polled[/] {total} unsettled PR row(s)")
            for name, n in tally.items():
                console.print(f"  {escape(name):<18} {n}")
            if tally.get(UNKNOWN):
                console.print(
                    "[dim]`unknown` rows stay unsettled and will be re-polled "
                    "next time; they are never counted as merged.[/]")

    asyncio.run(_go())


@cli.command("recall")
@click.argument("query")
@click.option("--limit", default=8, help="Max matches to show.")
@click.option("--include-pending", is_flag=True,
              help="Also search UNCONFIRMED memory proposals (the `nh learnings` "
                   "queue). Excluded by default because the coder is told to run "
                   "`nh recall` from Bash, and an unconfirmed proposal reaching it "
                   "that way would be a rule no human ever confirmed. For an "
                   "operator triaging the queue by hand, not for a run.")
@click.option("--all-projects", is_flag=True,
              help="Search memories from EVERY project. By default, when run "
                   "inside a git checkout, memories are scoped to that "
                   "project (by remote identity) plus globals — the B4 "
                   "boundary: one repo's lessons do not surface in another. "
                   "For an operator browsing the whole store, not for a run.")
def recall(query, limit, include_pending, all_projects):
    """Search past tasks, attempts, memories, and ingested history for prior
    work similar to QUERY — so the agent (via Bash: `nh recall <query>`) or a
    human can find how something like this was solved before.

    Plain keyword substring matching over what's already stored — agentic
    grep, not RAG (no embeddings, no new dependency, no index to keep fresh).

    MEMORIES ARE CONFIRMED-ONLY BY DEFAULT. `learning/queue.py` and
    `brain/store.py` both treat the human confirm step as load-bearing: a
    proposal is inert until a human confirms it in `nh learnings`. This command
    is named in the coder's own instructions as a Bash command it may run, so
    listing memories unfiltered here would hand the queue's unconfirmed
    proposals straight to a run — the confirm gate, bypassed by a search box.
    `--include-pending` is the operator's opt-in, and labels what it adds.
    """
    config, _ = _bootstrap(require_auth=False)
    terms = [t.lower() for t in query.split() if t]

    def _hit(*texts: str | None) -> bool:
        hay = " ".join(t for t in texts if t).lower()
        return bool(hay) and any(t in hay for t in terms)

    async def _go():
        async with Store(config.db_path) as store:
            rows: list[tuple[str, str, str]] = []  # (kind, id, summary)

            for task in await store.list_tasks():
                if not _hit(task.title, task.description):
                    continue
                attempts = await store.list_attempts(task.id)
                last = attempts[-1] if attempts else {}
                outcome = last.get("failure_reason") or task.status.value
                pr = next((a.get("pr_url") for a in reversed(attempts) if a.get("pr_url")), None)
                summary = f"{task.title}  ({task.status.value}: {outcome})"
                if pr:
                    summary += f"  {pr}"
                rows.append(("task", task.id[:8], escape(summary)))

            # B4: run from inside a checkout, memories are scoped to THAT
            # project (remote identity first, checkout path for legacy rows)
            # plus explicit globals — this command is named in the coder's own
            # instructions, so an unscoped search would be another project's
            # lessons reaching a run through a search box. Outside any git
            # repo (an operator's shell), or with --all-projects, no scoping.
            mem_scope: dict = {}
            if not all_projects:
                from ..learning.scope import repo_root, resolve_project_scope
                root = repo_root(os.getcwd())
                if root:
                    mem_scope = {"project": root,
                                 "scope": resolve_project_scope(root)}
            for mem in await store.list_memories(
                    confirmed=None if include_pending else True, **mem_scope):
                if not _hit(mem.get("title"), mem.get("content")):
                    continue
                kind = "memory" if mem.get("confirmed") else "memory (pending)"
                rows.append((kind, mem["id"][:8],
                            escape(f"({mem.get('type')}) {mem.get('content', '')[:100]}")))

            for h in await store.list_history_cache():
                if not _hit(h.get("title"), h.get("findings_json")):
                    continue
                rows.append(("history", h["cascade_id"][:8], escape(h.get("title") or "(untitled)")))

            if not rows:
                console.print(f"[dim]no matches for {query!r}[/]")
                return
            table = Table(title=f"recall: {query!r} ({len(rows)} match(es))")
            table.add_column("kind")
            table.add_column("id")
            table.add_column("summary")
            for kind, rid, summary in rows[:limit]:
                table.add_row(kind, rid, summary)
            console.print(table)
            if len(rows) > limit:
                console.print(f"[dim]…and {len(rows) - limit} more (raise --limit)[/]")

    asyncio.run(_go())


# The token groups an attempt records — one per NAMED ROLE, each with
# (used, cache_read, cache_creation). `Store.list_attempts` is SELECT *, so
# every one is already in hand.
#
# IMPORTED, not re-typed. This was a four-literal tuple beside four more
# literal tuples in metrics.py, api/models.py, eval/northstar.py and
# eval/replay.py, and the burn figure this file prints is only as complete as
# the shortest of them. Registering a role in `db.USAGE_ROLES` now widens all
# five together.
_TOKEN_GROUPS = tuple(USAGE_ROLES)
_TOKEN_KINDS = ("tokens_used", "cache_read_tokens", "cache_creation_tokens")


def _attempt_role_burn(a: dict) -> "dict[str, int]":
    """``{role: burn}`` for one attempt row — the same columns
    ``_attempt_tokens``'s ``burn`` adds up, kept apart instead of summed.

    The partition is exact by construction: every column in ``burn`` belongs
    to exactly one role here, so ``sum(_attempt_role_burn(a).values())``
    equals that ``burn`` for every row. Nothing chooses which roles are
    "interesting"; the caller decides what to print.
    """
    return {
        role: sum(int(a.get(f"{tier}{k}") or 0) for k in _TOKEN_KINDS)
        for tier, role in USAGE_ROLES.items()
    }


def _attempt_tokens(a: dict) -> "tuple[int | None, int | None]":
    """``(spend, burn)`` for one attempt row, or ``(None, None)`` when the
    CODER tokens are unknown.

    Both numbers are gated on ``tokens_used`` even though ``burn`` could in
    principle be computed without it: the coder columns are written at one
    point and the review columns by a separate later update, so a row can have
    a known burn and an unknown spend. Reporting a burn that silently excludes
    the coder would be a partial number presented as a total — the exact
    defect this function exists to prevent — so an unknown coder makes both
    unknown. Empirically moot (all 13 NULL rows have every other bucket at 0),
    and it under-claims rather than over-claims.

    spend — RAW ``tokens_used + cache_read_tokens`` on the CODER session only.

            NO LONGER what the budget guard enforces, and this docstring said
            it was until 2026-07-31. The guard now compares a COST-WEIGHTED
            sum (fresh x1.0, cache write x1.25, cache read x0.1 —
            ``core.pricing``) across every registered role, cache creation
            included, so this number and the cap are different quantities in
            different units: on the attempt that killed task d6e4b72a, this
            prints 6,591,126 where the blocker says 877,127. Neither is wrong;
            they answer different questions. Print it as RAW coder spend and
            never as "how much of the budget is gone" — for that, read the
            ``lifetime_budget`` event's ``tokens_weighted`` field.
    burn  — every token the attempt actually consumed: every role, all
            three buckets. This is what ``web/src/cost.js`` ``taskBurn`` sums,
            so the CLI and the board cannot disagree.

    Keeping them separate is the point. ``tokens_used`` alone is NON-CACHE
    CODER tokens, which under-reported a live runaway by ~5500x (an attempt
    aborted at 4,054,229 displayed as 731). But the coder's own total is not
    the whole story either: on that same attempt the plan and utility sessions
    added 740,643 tokens — 15% of the tokens and 34% of the dollars — so
    presenting it as the total is the same defect one tier up. cost.js's own
    header records this repo shipping that mistake twice.
    """
    if a.get("tokens_used") is None:
        return None, None
    spend = int(a["tokens_used"]) + int(a.get("cache_read_tokens") or 0)
    burn = sum(int(a.get(f"{g}{k}") or 0)
               for g in _TOKEN_GROUPS for k in _TOKEN_KINDS)
    return spend, burn


@cli.command("agents")
@click.option("--all", "show_all", is_flag=True,
              help="Include recently completed agents (last 24h), not just active.")
def agents(show_all):
    """Show active agent sessions — tasks currently being worked by the agent."""
    config, _ = _bootstrap(require_auth=False)
    active_statuses = {
        TaskStatus.IMPLEMENTING, TaskStatus.PLANNING, TaskStatus.CONTEXT,
        TaskStatus.REVIEWING, TaskStatus.TESTING,
    }

    async def _go():
        async with Store(config.db_path) as store:
            tasks = await store.list_tasks()
            active = [t for t in tasks if t.status in active_statuses]
            recent: list[Task] = []
            if show_all:
                from datetime import datetime, timezone, timedelta
                cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                for t in tasks:
                    if t.status not in active_statuses and t.updated_at:
                        try:
                            updated = t.updated_at
                            if isinstance(updated, str):
                                updated = datetime.fromisoformat(updated)
                            if updated.tzinfo is None:
                                updated = updated.replace(tzinfo=timezone.utc)
                            if updated > cutoff:
                                recent.append(t)
                        except (ValueError, AttributeError):
                            pass

            if not active and not recent:
                console.print("[dim]No active or recent agent sessions.[/]")
                return

            table = Table(title="Agent Sessions")
            table.add_column("id", style="bold")
            table.add_column("status")
            table.add_column("kind", style="magenta")
            table.add_column("att", justify="right", style="dim")
            table.add_column("turns", justify="right", style="dim")
            table.add_column("burn", justify="right", style="dim")
            table.add_column("title")
            table.add_column("repo", style="cyan")

            for t in active + recent:
                attempts = await store.list_attempts(t.id)
                att_n = str(len(attempts))
                last_turns = "—"
                last_tokens = "—"
                for a in reversed(attempts):
                    if a.get("turns_used") and last_turns == "—":
                        last_turns = str(a["turns_used"])
                    # BURN, not `tokens_used`. This column carried the same
                    # 5500x under-report as `nh logs` did — and this is the
                    # table an operator watches a runaway on, so it is the
                    # worst place to show the smallest number.
                    if last_tokens == "—":
                        _, _b = _attempt_tokens(a)
                        if _b:
                            last_tokens = f"{_b:,}"
                repo_name = t.repo_path.rstrip("/").rsplit("/", 1)[-1] if t.repo_path else ""
                status_str = t.status.value
                status_colors = {
                    "implementing": "bold green",
                    "planning": "blue",
                    "context": "blue",
                    "reviewing": "yellow",
                    "testing": "yellow",
                    "done": "dim green",
                    "failed": "dim red",
                    "escalated": "dim red",
                    "awaiting_approval": "dim yellow",
                }
                color = status_colors.get(status_str, "dim")
                styled = f"[{color}]{status_str}[/]" if color else status_str
                table.add_row(
                    t.id[:8], styled, t.kind, att_n, last_turns, last_tokens,
                    t.title[:50], repo_name[:20],
                )
            console.print(table)

    asyncio.run(_go())


@cli.command("unblock")
@click.argument("task_id")
@click.option("--fail", is_flag=True, help="Abandon the task (mark failed) instead of resuming.")
def unblock(task_id, fail):
    """Manually clear a block: resume to implementing, or --fail to abandon."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            if t.status == TaskStatus.DONE:
                console.print(f"[red]task is already done[/] — cannot unblock {t.id[:8]}")
                sys.exit(1)
            if t.status == TaskStatus.FAILED and (t.context or {}).get("cancel_reason"):
                console.print(f"[red]task is cancelled[/] — cannot unblock {t.id[:8]}")
                sys.exit(1)
            target = TaskStatus.FAILED if fail else TaskStatus.IMPLEMENTING
            # 🔴 Only a PARKED task may be unblocked into the loop. Without this
            # the command fired on a LIVE attempt (implementing / reviewing /
            # testing / awaiting_approval) and re-entered it, which is how the
            # checkpoint read below became a fail-OPEN hole: a sha the WAKE
            # WATCHER had chosen was re-applied and relabelled `human`, the
            # zero-diff honesty gate was disarmed, and an attempt that edited
            # nothing was credited and advanced to a PR. Reproduced end to end.
            # The drawer's Resume endpoint has always had this guard; this
            # command claimed parity with it while copying neither of the two
            # guards that make its checkpoint read safe.
            if target is TaskStatus.IMPLEMENTING and t.status not in _PARKED:
                console.print(
                    f"[yellow]task is {t.status.value}[/] — only parked tasks "
                    f"(blocked/awaiting_input/paused_quota/escalated) can be "
                    f"unblocked; use `nh reject` to send a live task back")
                return
            prior_status = t.status
            prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
            t.wake_check_at = None
            if target is TaskStatus.IMPLEMENTING:
                # Re-entering the loop by hand IS a human gate, so record whose
                # resume this is — otherwise the previous actor's `by` describes
                # it. Not done on the `--fail` path, which parks rather than
                # resumes.
                #
                # Read the blocker's checkpoint, then CLEAR the blocker — the
                # second guard the drawer has. A checkpoint must be consumable
                # exactly ONCE by the human who read it; leaving the blocker in
                # place made the same machine-chosen sha re-appliable forever,
                # every time stamped `human`.
                from ..blockers import resume_checkpoint, resume_provenance
                checkpoint = resume_checkpoint(t.blocker)
                t.blocker = None
                await store.update_task(t)
                t.context = await store.merge_context(
                    t.id,
                    {"resume_from": resume_provenance(checkpoint, "human")})
                # A human re-entering the loop withdraws any pending stop, or
                # the next attempt honours it on turn zero and parks straight
                # back (same as `nh task retry` / `nh reply`; pinned by the
                # re-entry registry's withdraws-a-pending-stop invariant).
                # ONLY on this branch: `--fail` parks, and is legal on a LIVE
                # task, where the flag may be the pause its worker is about to
                # honour — clearing it there would let the coder run on.
                await store.clear_cancel_request(t.id)
            else:
                await store.update_task(t)
            from ..blockers import human_event
            await store.set_status(
                t, target, validate=False,
                event=human_event(
                    "unblock", prior_status=prior_status, prior_blocker=prior_blocker,
                    actor="cli"),
            )
            console.print(f"[green]{t.id[:8]} -> {target.value}[/]")

    asyncio.run(_go())


# --------------------------------------------------------------------------- #
# Human-action verbs (PLAN.md Part 6)                                         #
# --------------------------------------------------------------------------- #

def _review_pass_evidence(context: dict, head_sha: str, repo) -> tuple[bool, str]:
    """(passed, evidence-line) for the branch's HEAD sha — the precondition
    `land_task` itself does not (and must not) check: it would need
    `Orchestrator._rounds_for_head`, and `vcs/` sits below `core/`
    (`core.orchestrator` already imports `vcs` at module scope, so the
    reverse import here would be circular). Local import keeps that
    one-way."""
    from ..core.orchestrator import Orchestrator

    history = (context or {}).get("review_history") or []
    if isinstance(history, str):
        import ast
        try:
            history = ast.literal_eval(history)
        except (ValueError, SyntaxError):
            history = []
    if not isinstance(history, list):
        history = []
    rounds = Orchestrator._rounds_for_head(history, head_sha=head_sha, repo=repo)
    if not rounds:
        return False, "no review round is stamped with a commit reachable from the branch head"
    last = rounds[-1] if isinstance(rounds[-1], dict) else {}
    passed = bool(last.get("passed"))
    verdict = "PASS" if passed else "not passed"
    evidence = f"review {verdict} on {head_sha[:12]} after {len(rounds)} round(s)"
    return passed, evidence


def _ready_batch_non_merge_message(tag, outcome):
    """Per-tag message for a `--ready --yes` step that is not a hard
    failure and did not land a merge — mirrors the single-task
    `nh approve <task_id>` wording for the same tag (`_approve_go_single`
    below) so the two paths stay in sync, condensed to one line for the
    batch listing."""
    result = outcome["result"]
    branch = outcome["branch"]
    pr_url = outcome["pr_url"]
    if tag == "already_satisfied":
        return "already satisfied claim confirmed; task done."
    if tag == "no_pr":
        return "merge the PR in your git host (no PR URL recorded)."
    if tag == "no_branch":
        return f"merge the PR in your git host (no branch/repo recorded). PR: {pr_url}"
    if tag == "already_landed":
        return f"content already on the default branch; task done. PR: {pr_url}"
    if tag == "unresolved_head":
        return (
            f"the branch {branch!r} could not be resolved locally; "
            f"merge the PR in your git host. PR: {pr_url}"
        )
    if tag == "skipped":
        return (result.message if result is not None and result.message
                else "merge the PR in your git host.") + f" PR: {pr_url}"
    return tag


async def _approve_find_ready(store, config):
    """Discover every AWAITING_APPROVAL task whose merge-policy verdict is
    ready for its CURRENT head sha — re-resolving the head so a verdict
    stamped for an older commit, or one whose policy file changed in the
    diff, is excluded. Returns [(task, pr_url, rules_passed, rules_total)]
    in the order `store.list_tasks()` returned them (discovery order)."""
    from ..vcs.git import GitError, GitRepo
    from ..vcs.task_pr import resolve_task_pr

    tasks = await store.list_tasks()
    candidates = [t for t in tasks if t.status == TaskStatus.AWAITING_APPROVAL]

    ready = []
    for t in candidates:
        resolved = await resolve_task_pr(store, t)
        branch = resolved.branch
        if not branch or not t.repo_path:
            continue
        git_cfg = config.get("git") or {}
        try:
            repo = GitRepo(
                Path(t.repo_path),
                identity_name=git_cfg.get("agent_identity_name", "no_human"),
                identity_email=git_cfg.get("agent_identity_email", "no-human@acme.com"),
                never_push_to=git_cfg.get("never_push_to")
                or ["main", "master", "release/*"],
            )
            repo.fetch()
            ref = repo.resolve_commitish(branch)
            head_sha = repo._run("rev-parse", ref) if ref else ""
        except (GitError, OSError):
            head_sha = ""
        if not head_sha:
            continue
        mp = ((t.context or {}).get("merge_policy") or {}).get(head_sha)
        if not isinstance(mp, dict):
            continue
        if mp.get("ready") is not True or mp.get("policy_changed_in_diff"):
            continue
        rules = mp.get("rules") or []
        total = len(rules)
        passed = sum(1 for r in rules if isinstance(r, dict) and r.get("passed"))
        ready.append((t, resolved.url, passed, total))
    return ready


async def _approve_go_ready(config, assume_yes, land_one):
    """`nh approve --ready [--yes]` — list every merge-ready
    awaiting_approval task; with `--yes`, land them one at a time through
    `land_one` (the same procedure a plain `nh approve <task_id>` uses),
    stopping at the first hard failure. `land_one` is the caller's
    `_land_one` closure — kept nested in `approve` itself so the one
    `land_task` call site in this command stays attributed to `approve`
    for `test_land_task_is_referenced_only_by_cli_and_api`."""
    async with Store(config.db_path) as store:
        ready = await _approve_find_ready(store, config)

        if not ready:
            console.print("[dim]no awaiting_approval task is merge-ready for its current head.[/]")
            return

        for t, pr_url, passed, total in ready:
            console.print(
                f"{t.id[:8]} · {t.title} · rules {passed}/{total} · {pr_url}"
            )

        if not assume_yes:
            console.print(
                f"\n{len(ready)} task(s) merge-ready — re-run with "
                "--yes to land them one at a time."
            )
            return

        console.print("")
        landed = 0
        # Only "precondition" and "failed" are hard failures — the same two
        # tags single-task `nh approve <task_id>` maps to sys.exit(1)
        # (`_approve_go_single`). Every other tag ("already_satisfied",
        # "no_pr", "no_branch", "already_landed", "unresolved_head",
        # "skipped") is a normal non-merge success there (approval
        # recorded; nothing to auto-merge, or gh/approve_merge is
        # unavailable) — the batch must keep walking, not stop, or
        # `--ready --yes` would abort at task 1 on any host without
        # `gh` installed even though nothing actually failed.
        for t, pr_url, passed, total in ready:
            outcome = await land_one(store, t)
            tag = outcome["tag"]
            result = outcome["result"]
            if tag in ("precondition", "failed"):
                step = result.step if result is not None else tag
                detail = result.stderr if (result is not None and result.stderr) else outcome["evidence"]
                console.print(
                    f"[bold red]stopped at[/] {t.id[:8]} — step {step!r}"
                    + (f":\n{detail}" if detail else "")
                )
                console.print(f"landed {landed}/{len(ready)} before stopping.")
                sys.exit(1)
            if tag == "done":
                landed += 1
                console.print(
                    f"[bold green]merged[/] {t.id[:8]} — landed "
                    f"{result.landed_sha[:12]}"
                )
                continue
            console.print(
                f"[bold green]approved[/] {t.id[:8]} — "
                f"{_ready_batch_non_merge_message(tag, outcome)}"
            )


async def _approve_go_landed(config, task_id, landed_sha, justification, base_branch):
    """`nh approve <task_id> --landed <sha> --because ...` — the human
    landed-override path (no `land_task` call: containment already
    refused, or the task never opened a PR)."""
    from ..blockers.landed_override import OverrideRefused, approve_landed_override
    async with Store(config.db_path) as store:
        t = await store.find_task(task_id)
        if not t:
            print_no_task_matching(task_id)
            sys.exit(1)
        try:
            result = await approve_landed_override(
                store, t, landed_sha, justification or "",
                base=base_branch)
        except OverrideRefused as exc:
            console.print(f"[bold red]refused:[/] {exc.reason}")
            sys.exit(1)
        residue = result["residue"]
        residue_text = ", ".join(residue) if residue else "none"
        prior_note = (
            " (was failed, no PR)"
            if result.get("prior_status") == "failed" else ""
        )
        matched_branch = result.get("matched_branch")
        branch_note = f" on {matched_branch}" if matched_branch else ""
        console.print(
            f"[bold green]override recorded[/] — {t.id[:8]} completed"
            f"{prior_note} on human assertion that content landed at "
            f"{landed_sha[:12]}{branch_note}. residue: {residue_text}"
        )


async def _approve_go_single(config, task_id, land_one):
    """Plain `nh approve <task_id>` — the same procedure `--ready --yes`
    (`_approve_go_ready`) walks over several tasks, rendered as the
    single-task console output. `land_one` is the caller's `_land_one`
    closure (see `_approve_go_ready`'s docstring for why it stays nested)."""
    async with Store(config.db_path) as store:
        t = await store.find_task(task_id)
        if not t:
            print_no_task_matching(task_id)
            sys.exit(1)
        if t.status != TaskStatus.AWAITING_APPROVAL:
            console.print(
                f"[yellow]task is {t.status.value!r}, not awaiting_approval — cannot approve[/]"
            )
            sys.exit(1)

        outcome = await land_one(store, t)
        tag = outcome["tag"]
        pr_url = outcome["pr_url"]
        branch = outcome["branch"]
        evidence = outcome["evidence"]
        result = outcome["result"]

        if tag == "already_satisfied":
            console.print(
                f"[bold green]approved[/] {t.id[:8]} — already satisfied "
                "claim confirmed; no code change was needed. Task done."
            )
            return

        if tag == "no_pr":
            console.print(f"[bold green]approved[/] {t.id[:8]} — merge the PR in your git host.")
            console.print("  [dim](no PR URL recorded)[/]")
            return

        if tag == "no_branch":
            console.print(
                f"[bold green]approved[/] {t.id[:8]} — merge the PR in your "
                "git host (no branch/repo recorded to merge automatically)."
            )
            console.print(f"  PR: {pr_url}")
            return

        if tag == "already_landed":
            console.print(
                f"[bold green]approved[/] {t.id[:8]} — content is already "
                "on the default branch; task done (no merge attempted)."
            )
            console.print(f"  PR: {pr_url}")
            return

        if tag == "unresolved_head":
            # Can't even resolve the branch locally — there is nothing to
            # decide a merge from, so this is the same "auto-merge isn't
            # possible, merge it yourself" outcome as no branch/repo
            # recorded at all (above), not a hard failure: exit 0, the
            # approval stands, the task stays awaiting_approval.
            console.print(
                f"[bold green]approved[/] {t.id[:8]} — the branch {branch!r} "
                "could not be resolved in the local repo; merge the PR in "
                "your git host."
            )
            console.print(f"  PR: {pr_url}")
            return

        if tag == "precondition":
            console.print(
                f"[bold red]cannot merge:[/] preconditions — {evidence}. "
                "Approval recorded; task remains awaiting_approval."
            )
            sys.exit(1)

        if tag == "skipped":
            console.print(
                f"[bold green]approved[/] {t.id[:8]} — "
                f"{result.message or 'merge the PR in your git host.'}"
            )
            console.print(f"  PR: {pr_url}")
            return

        if tag == "failed":
            console.print(
                f"[bold red]merge FAILED[/] at step {result.step!r}:\n{result.stderr}"
            )
            if result.gate_reason:
                # The reason already rides in `result.stderr` when the
                # failure came from the test step itself, but a step-6a
                # (export_guard verify) or push failure would not otherwise
                # say which gate the run was headed for — echo it explicitly
                # so a full-gate failure is never mistaken for the (cheaper,
                # more common) focused one.
                console.print(f"  gate: {result.gate_reason}")
            console.print(
                "Approval recorded; task remains awaiting_approval. Fix the "
                "issue and re-run `nh approve`."
            )
            sys.exit(1)

        # tag == "done"
        console.print(
            f"[bold green]merged[/] {t.id[:8]} — landed "
            f"{result.landed_sha[:12]} onto the default branch. Task done."
        )
        if result.gate_reason:
            console.print(f"  gate: {result.gate_reason}")


@cli.command("approve")
@click.argument("task_id", required=False)
@click.option("--ready", "list_ready", is_flag=True, default=False,
              help="List every awaiting_approval task whose merge-ready "
                   "policy verdict is ready for its CURRENT head sha (a "
                   "verdict stamped for an older commit, or one whose "
                   "policy file changed in the PR, does not count), instead "
                   "of approving a single TASK_ID. Combine with --yes to "
                   "land them.")
@click.option("--yes", "assume_yes", is_flag=True, default=False,
              help="With --ready, land the listed tasks sequentially "
                   "through the same approve path as a plain `nh approve "
                   "<task_id>`, stopping at the first failure. Without it, "
                   "--ready only lists — nothing lands.")
@click.option("--landed", "landed_sha", default=None,
              help="Human landed-override: assert this task's content landed "
                   "at this commit (an ancestor of its base branch), when "
                   "automated containment refuses on a supervisor-adapted "
                   "squash train — or a task that failed before ever opening "
                   "a PR (e.g. budget exhaustion) whose content a human "
                   "later landed. Requires --because.")
@click.option("--because", "justification", default=None,
              help="Required with --landed: why a human is asserting this "
                   "landed rather than letting containment decide.")
@click.option("--base", "base_branch", default=None,
              help="The branch --landed's commit is asserted to be an "
                   "ancestor of. Required when the task never dispatched (no "
                   "base_branch was ever recorded) and has no project default "
                   "on record; optional otherwise — when given on a task that "
                   "already has a recorded or default base, it NARROWS the "
                   "check to exactly this branch instead of trying the usual "
                   "candidates. Must resolve to something that exists in the "
                   "repo (a branch, a tag, or a raw commit sha) — this tool "
                   "never guesses one. A raw sha is accepted but is its own "
                   "trivial ancestor, so passing the same value here as "
                   "--landed proves nothing; name a branch if you want the "
                   "check to mean something.")
def approve(task_id, list_ready, assume_yes, landed_sha, justification, base_branch):
    """Approve and merge — squash-lands the PR under the operator identity
    (the agent still never merges on its own)."""
    _refuse_agent_gate_act("approve")

    if list_ready and task_id:
        console.print(
            "[bold red]error:[/] --ready lists awaiting_approval tasks; it "
            "does not take a TASK_ID."
        )
        sys.exit(2)
    if assume_yes and not list_ready:
        console.print("[bold red]error:[/] --yes only applies together with --ready.")
        sys.exit(2)
    if list_ready and landed_sha is not None:
        console.print("[bold red]error:[/] --ready and --landed are mutually exclusive.")
        sys.exit(2)
    if not list_ready and not task_id:
        raise click.UsageError("Missing argument 'TASK_ID'.")

    config, _ = _bootstrap(require_auth=False)

    async def _land_one(store, t):
        """Run the approve→land procedure for one AWAITING_APPROVAL task —
        the body of `nh approve <task_id>` (below `--landed`/lane checks),
        extracted so `--ready --yes` can walk several tasks through the
        exact same path `nh approve <task_id>` uses one at a time. Never
        prints or calls sys.exit: both callers render their own console
        output from the returned dict.

        Returns {"tag": <outcome>, "pr_url": str, "branch": str,
        "evidence": str, "result": LandResult | None}. `tag` is one of
        "already_satisfied", "no_pr", "no_branch", "already_landed",
        "unresolved_head", "precondition", "skipped", "failed", "done".
        """
        t.context = await store.merge_context(
            t.id, {"approved_at": _now_iso(), "approval_superseded_at": None})
        # An already-satisfied claim has no PR to merge — approval IS the
        # human confirmation its terminal promised, so it completes here
        # (the agent still never merges anything; there is nothing to).
        # Guarded on `task_has_pr_evidence`, not `attempts.pr_url` alone
        # (live incident, task 8c8b36b5): a draft PR opened pre-review is
        # recorded only in `context["pr_draft_created"]` or a `pr_draft`
        # event, never on an attempt row — reading attempts alone missed
        # it and completed the task while its PR sat open. After a
        # send-back a LATER attempt may ship a real PR — that approval
        # must stay a merge instruction, never a false DONE (PR #101
        # round-2 review).
        pr_url = await task_has_pr_evidence(store, t)
        if (t.context or {}).get("already_satisfied_report") and not pr_url:
            from ..blockers import process_actor
            await store.set_status(
                t, TaskStatus.DONE, validate=False,
                event={"source": "human", "kind": "approved_already_satisfied",
                       "text": "already-satisfied claim confirmed by approve",
                       "actor": process_actor()},
            )
            return {"tag": "already_satisfied", "pr_url": pr_url, "branch": "",
                    "evidence": "", "result": None}

        if not pr_url:
            return {"tag": "no_pr", "pr_url": pr_url, "branch": "",
                    "evidence": "", "result": None}

        from ..vcs.approve_merge import land_task
        from ..vcs.git import GitError, GitRepo
        from ..vcs.task_pr import resolve_task_pr

        resolved = await resolve_task_pr(store, t)
        branch = resolved.branch
        if not branch or not t.repo_path:
            return {"tag": "no_branch", "pr_url": pr_url, "branch": branch or "",
                    "evidence": "", "result": None}

        from ..blockers.shipped import complete_if_approved_and_landed
        if await complete_if_approved_and_landed(
                store, t, pr_url, branch=branch) is not None:
            return {"tag": "already_landed", "pr_url": pr_url, "branch": branch,
                    "evidence": "", "result": None}

        git_cfg = config.get("git") or {}
        try:
            repo = GitRepo(
                Path(t.repo_path),
                identity_name=git_cfg.get("agent_identity_name", "no_human"),
                identity_email=git_cfg.get("agent_identity_email", "no-human@acme.com"),
                never_push_to=git_cfg.get("never_push_to")
                or ["main", "master", "release/*"],
            )
            repo.fetch()
            ref = repo.resolve_commitish(branch)
            head_sha = repo._run("rev-parse", ref) if ref else ""
        except (GitError, OSError):
            head_sha = ""

        if not head_sha:
            # Can't even resolve the branch locally — there is nothing to
            # decide a merge from, so this is the same "auto-merge isn't
            # possible, merge it yourself" outcome as no branch/repo
            # recorded at all (above), not a hard failure.
            return {"tag": "unresolved_head", "pr_url": pr_url, "branch": branch,
                    "evidence": "", "result": None}

        passed, evidence = _review_pass_evidence(t.context or {}, head_sha, repo)
        if not passed:
            return {"tag": "precondition", "pr_url": pr_url, "branch": branch,
                    "evidence": evidence, "result": None}

        tested = (await store.latest_attempt_branch(t.id)).get("commit_sha") or ""
        result = land_task(
            repo_path=t.repo_path, branch=branch, pr_url=pr_url,
            task_id=t.id, task_title=t.title, review_evidence=evidence,
            config=config.data, tested_commit_sha=tested,
        )

        if result.skipped:
            return {"tag": "skipped", "pr_url": pr_url, "branch": branch,
                    "evidence": evidence, "result": result}

        if not result.ok:
            return {"tag": "failed", "pr_url": pr_url, "branch": branch,
                    "evidence": evidence, "result": result}

        from ..blockers import process_actor
        await store.set_status(
            t, TaskStatus.DONE, validate=False,
            event={"source": "human", "kind": "human_merged",
                   "sha": result.landed_sha, "text": result.message,
                   "actor": process_actor()},
        )
        return {"tag": "done", "pr_url": pr_url, "branch": branch,
                "evidence": evidence, "result": result}

    if list_ready:
        asyncio.run(_approve_go_ready(config, assume_yes, _land_one))
        return

    if landed_sha is not None:
        asyncio.run(_approve_go_landed(config, task_id, landed_sha, justification, base_branch))
        return

    asyncio.run(_approve_go_single(config, task_id, _land_one))


@cli.command("review-comments")
@click.argument("task_id")
@click.option("--post", "post_spec", default=None,
              help="Approve + post drafts: 'all' or 1-based numbers like 1,3,5. "
                   "Omit to just list them (nothing is posted).")
def review_comments(task_id, post_spec):
    """Show a code-review task's DRAFT comments and approve them one-by-one or all.

    A code_review NEVER posts to the PR on its own — it parks the drafted
    comments here. This is the only path that posts them, and only the ones you
    name. Without --post it just lists; the PR is untouched until you approve.
    """
    config, _ = _bootstrap(require_auth=(post_spec is not None))

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            ctx = t.context or {}
            drafts = ctx.get("draft_review_comments") or []
            pr_url = ctx.get("pr_url")
            if not drafts:
                console.print(
                    f"[yellow]{t.id[:8]} has no draft review comments[/] "
                    "(not a finished code_review, or the review found no issues)."
                )
                return
            console.print(f"[bold]{len(drafts)} draft comment(s)[/] for [cyan]{pr_url}[/]\n")
            for i, d in enumerate(drafts, 1):
                mark = "[green]✓ posted[/]" if d.get("posted") else "[dim]draft[/]"
                loc = (f"{d.get('file')}:{d.get('line')}"
                       if d.get("file") and d.get("line") else "(general)")
                # The severity and the comment are MODEL-authored. Wrapping a
                # model string in square brackets does not decorate it — rich
                # parses it as a markup tag, so every realistic lowercase value
                # ("high", "medium", "blocking") was silently swallowed and only
                # an uppercase one survived, by accident of not being a valid
                # tag. The field a human reads first to triage was invisible.
                # escape() the value AND keep the brackets out of the markup.
                sev = (f" \\[{escape(str(d['severity']))}]"
                       if d.get("severity") else "")
                console.print(f"  [bold]{i}.[/] {mark} [cyan]{escape(loc)}[/]{sev}",
                              emoji=False)
                console.print(f"     {escape(str(d.get('comment') or ''))}\n",
                              emoji=False)
            if not post_spec:
                console.print(
                    "[dim]Approve + post with:  "
                    f"nh review-comments {t.id[:8]} --post all   (or --post 1,3)[/]"
                )
                return
            if post_spec.strip().lower() == "all":
                which = "all"
            else:
                try:
                    which = [int(x) - 1 for x in post_spec.split(",") if x.strip()]
                except ValueError:
                    console.print("[red]--post must be 'all' or numbers like 1,3,5[/]")
                    sys.exit(1)
            orch = _build_orchestrator(config, store, task=t)
            posted, remaining = await orch.post_draft_comments(t, which)
            console.print(f"[green]posted {posted}[/] comment(s); {remaining} still unposted.")
            if remaining == 0:
                console.print(f"[bold green]{t.id[:8]} done[/] — all approved comments posted.")

    asyncio.run(_go())


@cli.command("reject")
@click.argument("task_id")
@click.option("--reason", required=True, help="Feedback for the agent on the next attempt.")
def reject(task_id, reason):
    """Send a task back with feedback; agent retries on next run."""
    config, _ = _bootstrap(require_auth=False)
    from ..core.bounds import Bounds
    from ..core.budget_floor import check_budget_floor

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            if t.status == TaskStatus.DONE:
                console.print(f"[red]task is already done[/] — cannot reject {t.id[:8]}")
                sys.exit(1)
            if t.status == TaskStatus.FAILED and (t.context or {}).get("cancel_reason"):
                console.print(f"[red]task is cancelled[/] — cannot reject {t.id[:8]}")
                sys.exit(1)
            warning = await check_budget_floor(
                store, t, bounds=Bounds.from_config(config.get("bounds")))
            if warning is not None:
                Console(stderr=True).print(f"[yellow]{warning.message()}[/]")
            _sent_back_at = _now_iso()
            await store.append_context_list(t.id, "send_back_feedback",
                                            {"at": _sent_back_at, "message": reason})
            # The CLI twin of the drawer's "Send back" — same human gate, same
            # provenance stamp. No checkpoint is involved, so this CLEARS any
            # recorded sha rather than relabelling one it never chose.
            from ..blockers import human_event, record_pending_send_back, resume_provenance
            # Mark the send-back pending — cleared at the next `attempt_start`,
            # or named in the blocker if a loop-head gate refuses to start a
            # round at all (`orchestrator._refuse_round`).
            await record_pending_send_back(
                store, t, source="reject", message=reason, actor="cli",
                at=_sent_back_at)
            prior_status = t.status
            prior_blocker = t.blocker if isinstance(t.blocker, dict) else None
            # Twin of the endpoint, down to this line: a `resume_from` with no
            # sha reads to the orphan sweep exactly like none at all, so the
            # dead rows are retired or the sweep re-stamps over this decision.
            await store.close_open_attempts(t.id)
            t.context = await store.merge_context(
                t.id, {"resume_from": resume_provenance(None, "human")})
            # Sending back is a human re-entry: withdraw BOTH human-stop
            # signals — the cancel flag and the durable `blocker.human_stopped`
            # hold the board stamps on a paused task (the wake sweep skips it
            # and the card reads "stopped by you"), which a fresh run must not
            # carry. `nh unblock` and `nh task resume` drop the blocker the
            # same way.
            t.blocker = None
            t.wake_check_at = None
            await store.update_task_columns(t)
            await store.clear_cancel_request(t.id)
            await store.set_status(
                t, TaskStatus.IMPLEMENTING, validate=False,
                event=human_event(
                    "reject", prior_status=prior_status, prior_blocker=prior_blocker,
                    reason=reason, actor="cli"),
            )
            console.print(
                f"[yellow]sent back[/] {t.id[:8]} — run [bold]nh watch {t.id[:8]}[/] to retry."
            )

    asyncio.run(_go())


@cli.command("diff")
@click.argument("task_id")
def diff(task_id):
    """Show the git diff for the latest attempt's commit."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            if not t.repo_path:
                console.print("[yellow]no repo_path recorded for this task[/]")
                return
            attempts = await store.list_attempts(t.id)
            sha = next(
                (a["commit_sha"] for a in reversed(attempts) if a.get("commit_sha")),
                None,
            )
            if not sha:
                console.print("[dim]no commit recorded yet[/]")
                return
            import subprocess
            try:
                result = subprocess.run(
                    ["git", "diff", f"{sha}~1..{sha}", "--no-color"],
                    cwd=t.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode == 0:
                    console.print(result.stdout or "[dim](empty diff)[/]")
                else:
                    console.print(
                        f"[yellow]git diff failed:[/] {result.stderr.strip()}\n"
                        f"[dim]commit: {sha}  branch: "
                        f"{next((a['branch_name'] for a in reversed(attempts) if a.get('branch_name')), '?')}[/]"
                    )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                console.print(f"[red]git error:[/] {exc}")

    asyncio.run(_go())


@cli.command("review")
@click.argument("target")
@click.option("--repo", default=".",
              help="Local clone to fetch the PR diff from (for a PR/MR URL).")
def review(target, repo):
    """Review a PR, or show a task's review checklist.

    TARGET is a PR/MR URL — queues a standalone code_review task that fetches the
    diff, runs the fresh-context adversarial reviewer, and posts cited findings —
    OR a task id, which shows that task's latest review checklist.
    """
    import re as _re
    is_pr_url = bool(_re.match(r"https?://\S+/(?:pull|merge_requests)/\d+", target))
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            if is_pr_url:
                from ..core.task import Task
                from ..profile import apply_default_task_config
                t = Task.new(f"Review {target}",
                             repo_path=str(Path(repo).resolve()),
                             description=target, kind="code_review")
                profile = await store.get_profile(t.repo_path)
                t.config = apply_default_task_config(profile, t.config)
                await store.create_task(t)
                console.print(f"[green]queued code review[/] [bold]{t.id[:8]}[/] "
                              f"— {target}")
                console.print("  the worker fetches the diff, runs the adversarial "
                              "reviewer, and posts cited findings on the PR.")
                console.print(f"  see the result: [bold]nh review {t.id[:8]}[/]")
                return
            t = await store.find_task(target)
            if not t:
                print_no_task_matching(target)
                sys.exit(1)
            attempts = await store.list_attempts(t.id)
            attempt = next(
                (a for a in reversed(attempts) if a.get("review_checklist")), None
            )
            if not attempt:
                console.print("[dim]no review checklist yet[/]")
                return
            import json as _json
            raw = attempt["review_checklist"]
            checklist = _json.loads(raw) if isinstance(raw, str) else raw
            passed_overall = checklist.get("passed", False)
            verdict = "[green]PASSED[/]" if passed_overall else "[red]FAILED[/]"
            console.rule(f"[bold]review — attempt #{attempt['attempt_number']} — {verdict}")
            for item in checklist.get("items") or []:
                icon = "[green]✓[/]" if item.get("passed") else "[red]✗[/]"
                console.print(f"  {icon} {escape(str(item.get('label', '')))}",
                              soft_wrap=True, emoji=False)
                if item.get("evidence"):
                    console.print(f"    [dim]{escape(str(item['evidence']))}[/]",
                                  soft_wrap=True, emoji=False)

    asyncio.run(_go())


@cli.command("investigate")
@click.argument("question", required=False)
@click.option("--repo", default=".", help="Repo to investigate.")
@click.option("--show", "show_id", default=None,
              help="Print the report of a completed investigation instead.")
def investigate(question, repo, show_id):
    """Start a read-only investigation (root-cause / analysis) that produces a
    cited report — no PR, no test gate — for the questions the implement→PR loop
    can't converge on. Or --show a completed one's report.
    """
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            if show_id:
                t = await store.find_task(show_id)
                if not t:
                    print_no_task_matching(show_id)
                    sys.exit(1)
                findings = (t.context or {}).get("findings")
                if not findings:
                    console.print("[dim]no report yet — investigation not complete[/]")
                    return
                console.rule(f"[bold]investigation report — {t.title[:60]}")
                console.print(escape(str(findings)), soft_wrap=True, emoji=False)
                return
            if not question:
                console.print("[red]provide a question to investigate, or "
                              "--show <task_id>[/]")
                sys.exit(1)
            from ..core.task import Task
            from ..profile import apply_default_task_config
            t = Task.new(question, repo_path=str(Path(repo).resolve()),
                         kind="investigation")
            profile = await store.get_profile(t.repo_path)
            t.config = apply_default_task_config(profile, t.config)
            await store.create_task(t)
            console.print(f"[green]investigating[/] [bold]{t.id[:8]}[/] — {question}")
            console.print("  read-only; produces a cited report (no PR, no test gate).")
            console.print(f"  read it when done: [bold]nh investigate --show {t.id[:8]}[/]")

    asyncio.run(_go())


@cli.command("logs")
@click.argument("task_id")
def logs(task_id):
    """Show the attempt log for a task (turns, tokens, result, failure reason)."""
    config, _ = _bootstrap(require_auth=False)

    async def _go():
        async with Store(config.db_path) as store:
            t = await store.find_task(task_id)
            if not t:
                print_no_task_matching(task_id)
                sys.exit(1)
            console.print(
                f"[bold]{t.id[:8]}[/] [blue]{t.status.value}[/] — {t.title}"
            )
            attempts = await store.list_attempts(t.id)
            if not attempts:
                console.print("[dim]no attempts yet[/]")
                return
            # Per-attempt cache telemetry (coder-in-attempt cache-burn ticket).
            # A running (or aborted-before-exit) attempt has NOT yet had its
            # `cache_read_tokens`/`cache_creation_tokens` columns filled — those
            # write only at attempt exit — so it would otherwise print as free.
            # `cache_burn` task_events carry the running total mid-flight; the
            # newest one per attempt_number is the live figure, and its
            # `compactions` count is the observable proof the AC1 config fix
            # actually fired.
            _cache_events = {}
            for _e in await store.list_events(t.id):
                if _e.get("kind") != "cache_burn":
                    continue
                _an = _e.get("attempt_number")
                _prev = _cache_events.get(_an)
                if _prev is None or (_e.get("ts") or 0) >= (_prev.get("ts") or 0):
                    _cache_events[_an] = _e
            for a in attempts:
                import json as _json
                tr_raw = a.get("test_results")
                tr = (_json.loads(tr_raw) if isinstance(tr_raw, str) else tr_raw) or {}
                status_color = "green" if a.get("status") == "succeeded" else "red"
                # SPEND, not just non-cache tokens. `tokens_used` holds
                # non-cache only, and cache reads are the overwhelming
                # majority of real burn. Printing the former alone
                # under-reported a live runaway by ~5500x: an attempt that was
                # aborted for spending 4,054,229 tokens displayed as
                # `tokens=731`, so the one attempt that needed attention
                # looked like the cheapest thing that ever ran.
                #
                # These are RAW token counts. They are no longer the quantity
                # the budget guard compares — since 2026-07-31 that is a
                # cost-weighted sum over every role (`core.pricing`), and
                # is ~5x smaller. See `_attempt_tokens`.
                # TWO numbers, because they answer different questions and
                # collapsing them lies about one of them. See _attempt_tokens.
                _spend, _burn = _attempt_tokens(a)
                _fmt = lambda v: f"{v:,}" if v is not None else "?"
                _plain = a.get("tokens_used")
                # The newest `cache_burn` event for THIS attempt, if any —
                # the live figure for a running (or aborted-before-exit)
                # attempt whose persisted cache columns are still 0/NULL, and
                # the source of the compaction count either way (AC2).
                _live = _cache_events.get(a.get("attempt_number"))
                _compactions = (_live or {}).get("compactions") or 0
                console.print(
                    f"\n  [bold]attempt #{a['attempt_number']}[/] "
                    f"[{status_color}]{a.get('status', '?')}[/]  "
                    # `.get(k, '?')` defaults only on a MISSING key, never on a
                    # NULL value — and an aborted attempt records turns as
                    # NULL, so this printed the literal "turns=None".
                    f"turns={a.get('turns_used') if a.get('turns_used') is not None else '?'}  "
                    # "tok", because every other cost surface here prints
                    # dollars and a bare number reads as money.
                    f"spend={_fmt(_spend)} tok  burn={_fmt(_burn)} tok "
                    f"[dim](raw)[/]\n"
                    f"    [dim]coder: non-cache {_fmt(_plain)} · cache-read "
                    f"{a.get('cache_read_tokens') or 0:,} · cache-creation "
                    # Was "(not counted by the cap)". It IS counted, and was
                    # even before the re-pricing — the sink and the lifetime
                    # ledger have summed cache creation since the twelve-column
                    # fix. It is now counted at 1.25x, the dearest of the three.
                    f"{a.get('cache_creation_tokens') or 0:,}"
                    + (f" · compactions {_compactions:,}" if _compactions else "")
                    + "[/]"
                )
                # A running/aborted-before-exit attempt has 0/NULL in the
                # persisted cache columns above — the row only fills in at
                # attempt EXIT — which used to read as "cheap" for exactly the
                # attempts that most need attention (the "nh logs hides cache
                # burn" class: tokens=731 for a 4M-token attempt). Never print
                # a bare 0 here: fail loud with either the live figure or an
                # explicit "not yet reported".
                if not (a.get("cache_read_tokens") or a.get("cache_creation_tokens")):
                    if _live is not None:
                        console.print(
                            f"    [yellow]cache (live): read "
                            f"{_live.get('cache_read', 0):,} · creation "
                            f"{_live.get('cache_creation', 0):,}[/]"
                        )
                    else:
                        console.print("    [dim]cache: not yet reported[/]")
                # WHERE the burn went, by named role. The line above breaks
                # out the coder alone, which answers "how much" and never
                # "which role" — and the roles are the only handle anyone has
                # on cost. Every role with a non-zero figure is listed and the
                # figures add up to `burn` exactly, so this is a
                # decomposition, not a selection: a role missing from the line
                # cost nothing, it was not judged uninteresting.
                _roles = {r: v for r, v in _attempt_role_burn(a).items() if v}
                if _roles:
                    console.print(
                        "    [dim]roles: "
                        + " · ".join(f"{r} {v:,}" for r, v in _roles.items())
                        + "[/]")
                if a.get("branch_name"):
                    console.print(f"    branch: {a['branch_name']}")
                if a.get("resume_checkpoint"):
                    # The other half of the same question. Green, and stated
                    # positively: this attempt continued from work that already
                    # existed, which is the only evidence anyone has that
                    # resuming works at all.
                    console.print(
                        f"    [green]resume: continued from "
                        f"{escape(str(a['resume_checkpoint'])[:8])}[/]")
                if a.get("resume_checkpoint_lost"):
                    # "why did this attempt start from scratch?" is asked here
                    # first, and nothing on the attempt row used to answer it.
                    # Yellow, not red: the attempt is not failed.
                    console.print(
                        f"    [yellow]resume: "
                        f"{escape(str(a['resume_checkpoint_lost']))}[/]")
                if a.get("pr_url"):
                    console.print(f"    PR:     {a['pr_url']}")
                if a.get("review_passed") is not None:
                    rv = "[green]pass[/]" if a["review_passed"] else "[red]fail[/]"
                    console.print(f"    review: {rv}")
                    # ...and WHY. A bare pass/fail bit is the one thing this
                    # command could already say, and it left the operator with
                    # no way to learn what the gate actually objected to
                    # without opening the database or the web UI. The cited
                    # findings are already on the attempt — print them.
                    from ..review.reviewer import findings_from_checklist
                    blocking, advisory = findings_from_checklist(
                        a.get("review_checklist"))
                    for tag, colour, items in (
                        ("blocking", "red", blocking),
                        ("advisory", "yellow", advisory),
                    ):
                        for it in items:
                            where = f"{it.file}:{it.line}" if it.file and it.line \
                                else (it.file or "")
                            cite = f" [dim]({escape(where)})[/]" if where else ""
                            # Every field below is reviewer-authored prose, so
                            # it is ESCAPED: rich treats "[str]" as markup and
                            # silently drops it, and review evidence is full of
                            # `list[str]` / `items[0]`. The grade is printed
                            # without brackets for the same reason — "[medium]"
                            # was being eaten, and it is the one field that says
                            # whether the finding blocked the task.
                            sev = escape(it.severity or "unclassified")
                            console.print(
                                f"      [{colour}]{tag}/{sev}[/] "
                                f"{escape(it.label)}{cite}")
                            if it.evidence:
                                ev = " ".join(it.evidence.split())
                                clipped = ev[:400] + "…" if len(ev) > 400 else ev
                                console.print(f"        [dim]{escape(clipped)}[/]")
                if tr:
                    passed = tr.get("passed", 0)
                    failed = tr.get("failed", 0)
                    console.print(f"    tests:  {passed} passed / {failed} failed")
                # D1.1: the PR body's "How I verified this" pointer (and
                # docs/pr-body.md) tell a reader to run `nh logs <id>` for
                # the full command log — this is the one place that promise
                # must actually hold. The path is computed the SAME way the
                # writer (`Orchestrator._write_verification_artifact`) did,
                # so this can never disagree with what a PR body pointed at.
                _verif_path = Orchestrator._verification_artifact_path(
                    t.id, a.get("attempt_number"))
                if _verif_path.exists():
                    console.print(
                        "    verification log: "
                        f"{Orchestrator._display_path(str(_verif_path))}")
                    try:
                        _verif_lines = _verif_path.read_text(
                            encoding="utf-8").splitlines()
                    except OSError as exc:
                        console.print(
                            f"      [yellow]could not be read: {exc}[/]")
                    else:
                        _tail = _verif_lines[-20:]
                        if len(_verif_lines) > len(_tail):
                            console.print(
                                f"      [dim]…{len(_verif_lines) - len(_tail)} "
                                "earlier line(s) not shown; open the file "
                                "for the rest[/]")
                        for _ln in _tail:
                            console.print(f"      [dim]{escape(_ln)}[/]")
                else:
                    console.print(
                        "    [dim]verification log: not written for this "
                        "attempt[/]")
                if a.get("failure_reason"):
                    # Escaped for the same reason as the findings above: this
                    # string is largely the reviewer's own words, and it is
                    # THE "why did this fail" line.
                    console.print(
                        f"    [red]reason: {escape(str(a['failure_reason']))}[/]")

    asyncio.run(_go())


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Learning queue (PLAN.md 4.5)                                                #
# --------------------------------------------------------------------------- #

@cli.command("learnings-curate")
@click.option("--apply", "apply_llm", is_flag=True,
              help="Also apply the LLM-proposed archives/consolidations "
                   "(deterministic dedupe always applies).")
def learnings_curate(apply_llm):
    """Tidy the pending learning queue (D2 #3): archive duplicates, propose
    consolidations. Never deletes; confirmed memories are never touched; the
    human confirm gate stands."""
    config, _ = _bootstrap()
    from ..learning.curator import curate

    async def _llm(prompt: str) -> str:
        from ..agent.advisory import advisory_backend
        backend = advisory_backend(config.utility_model, role="distill")
        result = await backend.run(prompt, cwd=Path("."), max_turns=1,
                                   effort="low")
        return result.final_text or ""

    async def _go():
        async with Store(config.db_path) as store:
            report = await curate(store, llm_call=_llm, apply=apply_llm)
            console.print(
                f"[green]dedupe:[/] {report.duplicates_archived} archived · "
                f"[cyan]llm proposals:[/] {len(report.llm_archive_proposed)} "
                f"archive, {len(report.llm_consolidate_proposed)} consolidate"
                f"{' — APPLIED' if report.llm_applied else ' (dry: rerun with --apply)'}")
            for a in report.llm_archive_proposed[:15]:
                console.print(f"  [dim]archive {a.get('id')}: "
                          f"{escape(str(a.get('reason',''))[:80])}[/]", emoji=False)
            for c in report.llm_consolidate_proposed[:10]:
                console.print(f"  [dim]merge {c.get('ids')}: "
                          f"{escape(str(c.get('title',''))[:70])}[/]", emoji=False)

    asyncio.run(_go())


def _learning_evidence_line(raw) -> str | None:
    """One dim line summarizing a lesson's B3 structured evidence — what
    happened, in which task(s), citing the event — or None for rows written
    before the column (NULL reads "unrecorded", and printing nothing is
    honest where guessing is not). Every interpolated value is escaped: the
    evidence quotes model/repo text verbatim."""
    if not raw:
        return None
    try:
        ev = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(ev, dict):
        return None
    kind = str(ev.get("kind") or "")
    if kind == "supervisor_correction":
        tasks = [str(t)[:8] for t in (ev.get("task_ids") or [])[:4]]
        return escape(
            f"supervisor correction x{ev.get('count', '?')}"
            + (f" · task(s) {', '.join(tasks)}" if tasks else ""))
    if kind == "review_finding":
        n = len(ev.get("findings") or [])
        return escape(
            f"review finding · task {str(ev.get('task_id') or '?')[:8]}"
            f" · attempt {ev.get('attempt') if ev.get('attempt') is not None else '?'}"
            f" · round {ev.get('review_round') or '?'}"
            + (f" · {n} finding(s)" if n else ""))
    if kind == "task_outcome":
        return escape(
            f"task outcome · task {str(ev.get('task_id') or '?')[:8]}"
            f" · {ev.get('status') or '?'}")
    what = str(ev.get("what") or "")
    return escape(f"{kind or 'recorded'}: {what}"[:120]) if (kind or what) else None


async def _print_learning_harvest(q, harvest_project, distill) -> None:
    """Run both harvest passes (B2 supervisor corrections, then the
    escalation/reviewer-FAIL/tamper-trip pass from `learning/failures.py`)
    and print what each produced. Shared by `nh learnings --harvest` (once,
    by hand) and `HarvestJob` (`core/scheduler.py`, on a cadence inside
    `nh serve`) — same clustering, same dedupe key, same proposal-only
    output either way."""
    notes: list[str] = []
    written = await q.harvest_supervisor_corrections(
        project=harvest_project, distill=distill, note=notes.append,
    )
    for n in notes:
        console.print(f"  [dim]{escape(n)}[/]", emoji=False)
    console.print(
        f"[green]{len(written)} supervisor-correction proposal(s) "
        f"queued[/] — nothing is active until you confirm it below"
        if written else
        "[green]no new supervisor-correction proposals[/] — either "
        "no correction recurred, or they are already queued")
    # The failure-harvest pass: escalations, reviewer FAIL findings and
    # tamper trips, clustered and dedup-guarded the SAME way as the
    # supervisor-correction pass above — see `learning/failures.py`'s
    # module docstring for what each signal is and why this stays
    # proposals, never PRs.
    fail_notes: list[str] = []
    fail_written = await q.harvest_failure_signals(
        project=harvest_project, distill=distill, note=fail_notes.append,
    )
    for n in fail_notes:
        console.print(f"  [dim]{escape(n)}[/]", emoji=False)
    console.print(
        f"[green]{len(fail_written)} escalation/review-fail/tamper "
        f"proposal(s) queued[/] — nothing is active until you "
        "confirm it below"
        if fail_written else
        "[green]no new escalation/review-fail/tamper proposals[/] "
        "— either no escalation/reviewer FAIL/tamper trip "
        "recurred, or they are already queued")


@cli.command("learnings")
@click.option("--confirm", "confirm_id", default=None, help="Confirm a proposal by id.")
@click.option("--reject", "reject_id", default=None, help="Reject/delete a proposal by id.")
@click.option("--active", is_flag=True, help="Show the confirmed active rule set instead.")
@click.option("--harvest", is_flag=True,
              help="Aggregate recurring supervisor corrections (B2), "
                   "escalations, reviewer FAIL findings and tamper trips "
                   "into proposals, then show the queue. The same passes "
                   "`nh serve` runs on a cadence (harvest.interval_seconds) "
                   "unless --no-harvest — this runs them once, by hand.")
@click.option("--harvest-project", default=None,
              help="Limit --harvest to one repo path (default: every project).")
@click.option("--stale", is_flag=True,
              help="Show the retire? section: confirmed rules not injected "
                   "into a prompt lately. Suggestion only — nothing is ever "
                   "auto-archived; confirm with --retire <id>.")
@click.option("--days", default=30, show_default=True,
              help="How many days without a use makes a rule stale (--stale).")
@click.option("--usage", is_flag=True,
              help="Memory lifecycle A: per-memory use_count, last_used_at "
                   "and the outcome split (success/failure/cancelled/timeout) "
                   "of every ledgered injection. Read-only.")
@click.option("--retire", "retire_id", default=None,
              help="Archive a CONFIRMED (active) rule you no longer want "
                   "injected — the retire? suggestion's explicit yes. "
                   "Reversible; refuses an unconfirmed id.")
@click.option("--triage-templated", "triage_templated", is_flag=True,
              help="One-time flood-source triage: find pending proposals "
                   "the per-success templated skill producer wrote (no "
                   "evidence). Dry run by default.")
@click.option("--apply", "triage_apply", is_flag=True,
              help="With --triage-templated, actually archive the matched "
                   "rows (reversible) instead of only reporting them.")
@click.option("--limit", "triage_limit", default=500, show_default=True,
              help="Cap on rows --triage-templated archives in one run.")
def learnings(confirm_id, reject_id, active, harvest, harvest_project,
              stale, days, usage, retire_id, triage_templated, triage_apply,
              triage_limit):
    """Review the human-confirmed learning queue; confirm or reject proposals.

    Nothing enters the active rule set without your one-click confirm.

    ``--stale`` answers the question the confirm queue cannot: of the rules you
    already confirmed, which have ever actually reached a prompt? A rule is
    stamped when `Orchestrator._load_active_memories` injects it, so "stale"
    means fetched-and-not-triggered, or never fetched at all. It PRINTS, and
    only prints: confirmed memories are yours, `learning/curator.py` exempts
    them from every automatic action, and an unused rule is not a wrong rule —
    a repo you have not touched this month makes its rules briefly "stale"
    without making them bad. Use `--reject` if you actually want one gone.

    ``--harvest`` runs BOTH harvest passes: the B2 pass (every persisted
    supervisor ``correct`` decision) and the failure-harvest pass (every
    persisted escalation, reviewer FAIL finding and tamper trip — see
    ``learning/failures.py``). Each signal is clustered by (project, source,
    normalized gist) and a cluster seen twice or more becomes ONE proposal. A
    single occurrence is a one-off nudge and is never proposed — repetition,
    not isolation, is what marks a durable lesson. A record from a task with
    no repo_path is skipped and counted: a repo-less lesson would become a
    rule in EVERY project. `nh serve` runs the identical two passes on a
    cadence (`harvest.interval_seconds`, default 12h) unless started with
    `--no-harvest`; this flag runs them once, synchronously, by hand.

    Re-running is a no-op, and stays one after you have triaged: a cluster
    already queued is skipped by its dedupe key, and so is one you REJECTED —
    rejecting a proposal from any of these four sources archives it rather
    than deleting it, so your "no" survives the next harvest.

    ``--usage`` answers a different question than ``--stale``: not just
    whether a rule has EVER been injected, but how often, and what happened
    to the tasks it rode along with — joined from the `memory_uses` ledger
    (`Orchestrator._load_active_memories` writes it at injection,
    `run_task`'s finalizer fills the outcome once a task ends). The counts
    are CORRELATIONAL, not causal: a rule injected into a task that failed
    did not necessarily cause the failure.

    ``--retire <id>`` is the retire? section's explicit yes: archives a
    CONFIRMED rule (reversible), refusing anything not confirmed.

    ``--triage-templated`` is the one-time flood-source cleanup: finds
    pending proposals the per-success templated skill producer wrote (no
    evidence beyond "a task finished") and reports them. Dry run by default
    — pass ``--apply`` to actually archive them (reversible) and write a
    JSON receipt.
    """
    config, _ = _bootstrap(require_auth=False)
    from ..learning import LearningQueue

    async def _distill(prompt: str) -> str:
        """The utility tier — advisory, single-turn, exactly as B1 distils a
        review finding. A failure here degrades the lesson to the verbatim
        corrections; it never loses the cluster, which is why the exception is
        swallowed HERE rather than allowed to abandon the rest of the harvest.
        `nh learnings` runs with `require_auth=False`, so "no credential
        configured" is an ordinary way to reach this."""
        from ..agent.advisory import advisory_backend
        try:
            backend = advisory_backend(config.utility_model, role="distill")
            result = await backend.run(prompt, cwd=Path("."), max_turns=1,
                                       effort="low")
            return (result.final_text or "")[:600]
        except Exception as exc:  # noqa: BLE001 — advisory tier, never fatal
            console.print(f"  [yellow]distillation unavailable[/] ({exc}) — "
                          "proposing the corrections undistilled",
                          emoji=False)
            return ""

    async def _go():
        async with Store(config.db_path) as store:
            q = LearningQueue(store)
            if harvest:
                await _print_learning_harvest(q, harvest_project, _distill)
            if usage:
                rows = await store.memory_usage_report()
                if not rows:
                    console.print(
                        "[green]no memory has been injected into a prompt "
                        "yet[/] — nothing to report")
                    return
                console.rule(f"[bold]{len(rows)} memor{'y' if len(rows) == 1 else 'ies'} "
                             f"with recorded use")
                console.print(
                    "[yellow]Correlational metrics — not causal[/]: a rule "
                    "injected into a task that failed did not necessarily "
                    "cause the failure.\n", emoji=False)
                for m in rows:
                    used = (m.get("last_used_at") or "")[:19] or "never"
                    console.print(
                        f"[bold]{m['memory_id'][:8]}[/] [magenta]{m['type']}[/] "
                        f"{escape(m['title'])} — used {m['use_count']}x, "
                        f"last {used}, outcomes: "
                        f"{m['success_count']} success / "
                        f"{m['failure_count']} failure / "
                        f"{m['cancelled_count']} cancelled / "
                        f"{m['timeout_count']} timeout", emoji=False)
                return
            if stale:
                rows = await q.retire_candidates(days=days)
                total = len(await q.active())
                if not rows:
                    console.print(
                        f"[green]every one of the {total} active rule(s) has "
                        f"been used in the last {days} day(s)[/]")
                    return
                console.rule(f"[bold]retire? — {len(rows)} of {total} active "
                             f"rule(s) unused for {days}+ day(s)")
                for m in rows:
                    used = (m.get("last_used_at") or "")[:19]
                    # "never" here means NO RECORD, which is two different
                    # things — a rule that has genuinely never triggered, and
                    # one confirmed before `last_used_at` existed. Saying which
                    # is impossible from the row, so it says neither.
                    when = (f"[dim]last used {used}[/]" if used
                            else "[yellow]no recorded use[/]")
                    console.print(
                        f"[bold]{m['id'][:8]}[/] [magenta]{m['type']}[/] "
                        f"{escape(m['title'])} — {when}", emoji=False)
                    console.print(
                        f"  suggestion only: nh learnings --retire {m['id'][:8]}",
                        emoji=False)
                console.print(
                    "\n[dim]Nothing was changed. These are your confirmed "
                    "rules; a rule can be unused simply because you have not "
                    "worked in its project. Remove one with: "
                    "nh learnings --reject <id>[/]", emoji=False)
                return
            if retire_id:
                mem = await store.find_memory(retire_id)
                if not mem:
                    console.print(f"[red]no rule matching[/] {retire_id}")
                    return
                if not mem.get("confirmed"):
                    console.print(
                        f"[red]{mem['id'][:8]} is not a confirmed (active) "
                        f"rule[/] — reject it instead: "
                        f"nh learnings --reject {mem['id'][:8]}")
                    return
                if await q.retire(mem["id"]):
                    console.print(
                        f"[yellow]retired[/] {mem['id'][:8]} — reversible: "
                        f"UPDATE memories SET archived = 0 WHERE id = "
                        f"'{mem['id']}'")
                else:
                    console.print(
                        f"[dim]{mem['id'][:8]} was already archived[/]")
                return
            if triage_templated:
                from ..learning.retire import is_templated_success_proposal
                pending_rows = await q.pending()
                targets = [m for m in pending_rows
                          if is_templated_success_proposal(m)]
                before_pending = len(pending_rows)
                if not targets:
                    console.print(
                        "[green]no templated per-success proposals found[/] "
                        f"({before_pending} pending total)")
                    return
                by_project: dict[str, int] = {}
                for m in targets:
                    by_project[m.get("project") or "(unscoped)"] = (
                        by_project.get(m.get("project") or "(unscoped)", 0) + 1)
                console.rule(
                    f"[bold]{'archiving' if triage_apply else 'would archive'} "
                    f"{len(targets)} templated per-success proposal(s)[/] "
                    f"of {before_pending} pending")
                for proj, n in sorted(by_project.items(),
                                      key=lambda kv: -kv[1]):
                    console.print(f"  [dim]{escape(proj)}: {n}[/]", emoji=False)
                for m in targets[:15]:
                    console.print(f"  {m['id'][:8]}  {escape(m['title'])}",
                                 emoji=False)
                if len(targets) > 15:
                    console.print(f"  [dim]... and {len(targets) - 15} more[/]",
                                 emoji=False)
                if not triage_apply:
                    id_list = ", ".join(f"'{m['id']}'" for m in targets)
                    console.print(
                        "\n[dim]Dry run — nothing was changed. Re-run with "
                        "--apply to archive these (reversible). Reversal "
                        f"SQL: UPDATE memories SET archived = 0 WHERE id IN "
                        f"({id_list})[/]", emoji=False)
                    return
                reason = ("one-time triage 2026-08-12: templated per-success "
                          "proposal (flood source), no evidence — reversible")
                archived_ids = []
                for m in targets[:triage_limit]:
                    if await store.archive_memory(m["id"], reason):
                        archived_ids.append(m["id"])
                after_pending = len(await q.pending())
                console.print(
                    f"\n[green]archived {len(archived_ids)}[/] — pending "
                    f"{before_pending} -> {after_pending}")
                import json as _json
                import time as _time
                from ..config import NO_HUMAN_HOME
                receipts_dir = NO_HUMAN_HOME / "receipts"
                receipts_dir.mkdir(parents=True, exist_ok=True)
                receipt_path = receipts_dir / (
                    f"learning-triage-{int(_time.time())}.json")
                receipt_path.write_text(_json.dumps({
                    "reason": reason,
                    "archived_ids": archived_ids,
                    "before_pending": before_pending,
                    "after_pending": after_pending,
                }, indent=2))
                console.print(f"[dim]receipt: {receipt_path}[/]", emoji=False)
                return
            if confirm_id:
                mem = await store.find_memory(confirm_id)
                if not mem:
                    console.print(f"[red]no proposal matching[/] {confirm_id}")
                    return
                await q.confirm(mem["id"])
                console.print(f"[green]confirmed[/] {mem['id'][:8]} — now active")
                return
            if reject_id:
                mem = await store.find_memory(reject_id)
                if not mem:
                    console.print(f"[red]no proposal matching[/] {reject_id}")
                    return
                await q.reject(mem["id"])
                console.print(f"[yellow]rejected[/] {mem['id'][:8]}")
                return
            rows = await (q.active() if active else q.pending())
            if not rows:
                console.print("[green]no active rules[/]" if active
                              else "[green]no pending proposals[/]")
                return
            label = "active rule set" if active else "pending proposals (one-click confirm)"
            console.rule(f"[bold]{label}")
            for m in rows:
                # WHICH SIGNAL proposed it. A human confirming a queue that
                # now mixes two producers needs to know whether they are
                # looking at a reviewer's blocking finding or a supervisor
                # correction that fired N times — the confidence in each is
                # different. NULL on every row written before the column, and
                # printed as nothing rather than guessed at.
                origin = m.get("origin") or ""
                origin_tag = f" [cyan]({origin})[/]" if origin else ""
                console.print(
                    f"[bold]{m['id'][:8]}[/] [magenta]{m['type']}[/]"
                    f"{origin_tag} {m['title']}"
                )
                # BLAST RADIUS, on the same screen as the confirm command.
                # A memory with project=NULL is GLOBAL — `list_memories`
                # matches it with `(project = ? OR project IS NULL)`, so
                # confirming it injects it into every project's rules. The
                # queue used to print no scope at all, which made a global
                # rule and a repo-scoped one look identical at the moment of
                # deciding. B2 refuses to CREATE global rows from corrections;
                # this is so the human can see the ones that already exist.
                scope = (m.get("project") or "").strip()
                # B4: the checkout path is the readable half; the remote-hash
                # identity (`project_scope`) is what recall actually matches
                # on, shown truncated so two checkouts of one repo are
                # visibly ONE scope. A row with neither is GLOBAL.
                scope_id = (m.get("project_scope") or "").strip()
                if scope or scope_id:
                    line = f"  [dim]scope:[/] {escape(scope or '(by remote identity)')}"
                    if scope_id:
                        line += f" [dim]· {escape(scope_id[:16])}[/]"
                    console.print(line, emoji=False)
                else:
                    console.print(
                        "  [yellow]scope: GLOBAL — applies to every project[/]",
                        emoji=False)
                # B3: the structured evidence, one line — what happened, in
                # which task, citing the event — beside the prose that
                # narrates it. Absent (honestly) on rows that predate it.
                ev_line = _learning_evidence_line(m.get("evidence"))
                if ev_line:
                    console.print(f"  [dim]evidence:[/] [dim]{ev_line}[/]",
                                  emoji=False)
                for line in (m["content"] or "").splitlines():
                    if line.strip():
                        console.print(f"  [dim]{line}[/]")
                if not active:
                    console.print(
                        f"  confirm: nh learnings --confirm {m['id'][:8]}   "
                        f"reject: nh learnings --reject {m['id'][:8]}"
                    )

    asyncio.run(_go())


@cli.command("history")
@click.option("--days", default=30, help="How many days back to extract.")
@click.option("--output", "-o", default=None,
              help="Directory to write markdown transcripts to.")
@click.option("--analyze", is_flag=True,
              help="Analyze transcripts for user corrections and propose learnings.")
@click.option("--json-out", is_flag=True,
              help="Print transcripts as JSON to stdout (for piping).")
@click.option("--roots", multiple=True, type=click.Path(path_type=Path),
              help="Claude Code projects roots to read (repeatable). "
                   "Default: ~/.claude/projects AND ~/.claude-personal/projects.")
def history(days, output, analyze, json_out, roots):
    """Extract conversation history from EVERY source.

    Combines Claude Code sessions read from disk (both the enterprise and
    personal config dirs — always available) with Windsurf transcripts  # term-ok: real IDE names
    from a running IDE (best-effort; skipped with a note when no IDE runs).

    \b
    Examples:
      nh history                        # list conversations, all sources
      nh history -o ./transcripts       # write markdown files
      nh history --analyze              # propose learnings from corrections
      nh history --days 7 --json-out    # JSON to stdout
      nh history --roots ~/.claude-personal/projects   # one root only
    """
    from ..history.claude_code import extract_claude_code_transcripts
    from ..history.extractor import (
        IDENotRunningError,
        extract_transcripts,
        write_transcripts,
    )

    if json_out:
        mark_machine_output()

    # In --json-out mode stdout must be pure JSON (pipeable to jq); status
    # lines go to stderr instead.
    status_console = Console(stderr=True) if json_out else console

    transcripts = []
    try:
        transcripts += extract_transcripts(days=days)
    except (IDENotRunningError, ImportError) as exc:
        status_console.print(f"[dim]windsurf: skipped ({escape(str(exc))})[/]")  # term-ok: real IDE names

    try:
        transcripts += extract_claude_code_transcripts(
            days=days, roots=list(roots) or None)
    except Exception as exc:  # noqa: BLE001 — one bad root must not kill the run
        status_console.print(f"[red]claude code extract failed: {exc}[/]")

    if not transcripts:
        status_console.print(
            f"[yellow]no conversations found in the last {days} days[/]")
        if json_out:
            print("[]")
        return

    status_console.print(
        f"[green]extracted {len(transcripts)} conversations[/] "
        f"({sum(len(t.messages) for t in transcripts)} total messages)")

    if json_out:
        import json as _json
        from dataclasses import asdict
        print(_json.dumps([asdict(t) for t in transcripts], indent=2,
                          ensure_ascii=False))
        return

    if output:
        index_path = write_transcripts(transcripts, output)
        console.print(f"[bold]transcripts written to:[/] {output}")
        console.print(f"[bold]index:[/] {index_path}")
    else:
        table = Table(title=f"Conversations (last {days} days)")
        table.add_column("#", style="dim", width=4)
        table.add_column("Date", width=12)
        table.add_column("Source", width=14)
        table.add_column("Title")
        table.add_column("Msgs", justify="right", width=6)
        for i, t in enumerate(transcripts, 1):
            table.add_row(str(i), t.created[:10],
                          getattr(t, "source", "") or "windsurf", t.title,  # term-ok: internal source tag names the real IDE
                          str(len(t.messages)))
        console.print(table)

    if analyze:
        from ..history.analyzer import analyze_all
        findings = analyze_all(transcripts)
        if not findings:
            console.print("[green]no correction patterns found[/]")
            return

        console.print(f"\n[bold]{len(findings)} user corrections detected[/]")

        config, _ = _bootstrap(require_auth=False)
        from ..learning import ORIGIN_HISTORY, LearningQueue

        async def _propose():
            from ..learning.pii import contains_pii
            async with Store(config.db_path) as store:
                q = LearningQueue(store)
                proposed = 0
                dropped_pii = 0
                for f in findings:
                    # This path writes to the queue directly rather than through
                    # TranscriptIngester, so it needs the personal-data gate of
                    # its own — a gate that only covers one of two doors is not
                    # a gate. Dropped, never redacted (see learning/pii.py).
                    pii = contains_pii(f.title, f.content)
                    if pii is not None:
                        dropped_pii += 1
                        continue
                    # source="proposed", NOT "history". `pending()` — the queue
                    # the success line below tells you to review with — selects
                    # source="proposed", so every proposal this command ever
                    # made was counted, printed, and then invisible to
                    # `nh learnings`. The producer's name belongs in `origin`.
                    mid = await store.add_memory(
                        mem_type=f.category,
                        title=f.title,
                        content=f.content,
                        tags=f.tags,
                        project=f.source_transcript,
                        source="proposed",
                        origin=ORIGIN_HISTORY,
                        confirmed=False,
                        dedupe_key=f"history:{f.category}:{f.title}",
                    )
                    if mid:
                        proposed += 1
                        console.print(
                            f"  [magenta]{f.category}[/] {f.title[:60]}"
                        )
                if dropped_pii:
                    console.print(
                        f"[yellow]{dropped_pii} dropped[/] — they carried "
                        "personal data (address / phone / email / payment / "
                        "ID / date of birth), which is never a coding rule"
                    )
                console.print(
                    f"\n[green]{proposed} proposals queued[/] — "
                    "review with: nh learnings"
                )

        asyncio.run(_propose())


def _acquire_pid_lock() -> bool:
    """Write a PID lock file. Returns True if we got the lock, False if another
    instance is already running."""
    from ..config import NO_HUMAN_HOME, ensure_private_dir
    lock_path = NO_HUMAN_HOME / "nh.pid"
    ensure_private_dir(lock_path.parent)

    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text().strip())
            # Check if the old process is still alive — WITHOUT signalling it.
            alive = _probe_pid(old_pid)
        except (ValueError, OSError):
            alive = False  # stale/unreadable lock — treat as dead
        if alive is True:
            return False  # process alive → another instance running
        # None (another user's pid) keeps its previous meaning here: not ours
        # to reason about, so the lock is taken. Unchanged from the POSIX
        # behaviour, where PermissionError fell into the same branch.

    lock_path.write_text(str(os.getpid()))
    return True


def _release_pid_lock() -> None:
    from ..config import NO_HUMAN_HOME
    lock_path = NO_HUMAN_HOME / "nh.pid"
    try:
        if lock_path.exists():
            pid = int(lock_path.read_text().strip())
            if pid == os.getpid():
                lock_path.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


def _print_visual_walks(console, d) -> None:
    """Visual-proof walks, two layers printed as one block. First the
    DEPENDENCY layer: advisory-shaped like the codex row in `doctor` —
    `visual_walks_row` never touches `d`, so an optional feature being off
    can never flip `d.healthy` or the exit code. Printed unconditionally so
    a plain `nh doctor` is the one place that tells a customer install "the
    PR screenshots you were expecting can't run here, and how to fix that."
    Then the per-repo CONFIGURATION layer (no-human-67 follow-up): current
    state and, side-by-side, the suggested state when the repo has a
    detected `npm run dev` convention and isn't configured yet. Nothing
    prints when there are no known profiles — read-only and additive. A
    repo can be "enabled" here while the dependency row says unavailable:
    the two rows name different layers (config present vs playwright
    installed), and the dependency row already carries its remedy. The
    dependency row also carries a third, chromium-only state (package
    imports but no browser binary was found) — its own line already names
    `--fix-walks`, so the hint below stays gated on the package layer only."""
    from ..doctor import visual_walks_row

    wrow = visual_walks_row()
    walks_ok = wrow["available"] and wrow.get("chromium") == "present"
    walks_colour = "green" if walks_ok else "yellow"
    walks_hint = "" if wrow["available"] else "  [dim](nh doctor --fix-walks to enable)[/]"
    console.print(f"[{walks_colour}]{wrow['line']}[/]{walks_hint}")

    for row in d.ui_evidence:
        name = Path(row["repo_path"]).name
        if row["enabled"]:
            console.print(
                f"[bold]visual-proof walks[/] — {name}: [green]enabled[/] "
                f"({row['start_cmd']} → {row['base_url']})"
            )
        else:
            sug = row.get("suggestion")
            if sug:
                console.print(
                    f"[bold]visual-proof walks[/] — {name}: "
                    f"[yellow]not configured[/]  detected "
                    f"`{sug['start_cmd']}` on :{sug['port']}, enable?"
                )
            else:
                console.print(
                    f"[bold]visual-proof walks[/] — {name}: "
                    "[dim]not configured[/]"
                )


@cli.command("doctor")
@click.option("-v", "--verbose", is_flag=True,
              help="Show the per-mechanism lifetime-firings table.")
@click.option("--verify-auth", is_flag=True,
              help="Also make ONE cheap live call to prove the credential is "
                   "accepted, not merely present. Costs a few tokens, so it is "
                   "off by default.")
@click.option("--fix-walks", is_flag=True,
              help="Install playwright + chromium (~120MB) so visual-proof "
                   "walks can run. Always asks for consent before "
                   "downloading anything; combine with --dry-run to see "
                   "the plan without installing.")
@click.option("--dry-run", is_flag=True,
              help="With --fix-walks: print the install plan, install "
                   "nothing, never prompt.")
def doctor(verbose, verify_auth, fix_walks, dry_run):
    """Liveness check: which guarded mechanisms have actually ever fired.

    The system's worst bugs were silences, not crashes — TESTING dead for its
    entire life, a watcher that persisted nothing. This enumerates every
    mechanism's lifetime firings and flags the known silent-death patterns.

    \b
    Exit code (so `nh doctor || exit 1` in a pipeline actually fires):
      0  healthy — no contradictions, no evidence gaps
      1  at least one contradiction or evidence gap

    Advisories NEVER affect the exit code: they are prunable leftovers, and a
    gate that fails on benign conditions is a gate people delete.
    """
    from datetime import datetime

    from ..config import DEFAULT_AUTH_PROFILE
    from ..doctor import diagnose

    if dry_run and not fix_walks:
        raise click.UsageError(
            "--dry-run only makes sense together with --fix-walks.")

    config, _ = _bootstrap(require_auth=False)

    if fix_walks:
        # Consent-first provisioning: this branch never runs the (unrelated)
        # mechanism diagnosis below — it only decides whether playwright +
        # chromium get installed, and stops BEFORE any download without an
        # explicit "y". `--dry-run` never even reaches the prompt.
        from ..doctor import (
            WALKS_DOWNLOAD_SIZE,
            visual_walks_row,
            walks_install_plan,
            walks_plan_description,
        )

        row = visual_walks_row()
        if row["available"]:
            console.print(
                "[bold green]visual-proof walks[/] — already available, "
                "nothing to install.")
            # `visual_walks_row` only checks that playwright IMPORTS, not
            # that the chromium binary is present — that binary check used
            # to start `sync_playwright()`, which raises inside a running
            # asyncio loop and made every real caller see "unavailable".
            # So a package-present/binary-missing install now reads
            # "already available" here with nothing left to fix; name the
            # residual remedy so that gap doesn't become a silent dead end.
            chromium_remedy = " ".join(walks_install_plan()[-1])
            console.print(
                "[dim]If a walk still produces no screenshots, the browser "
                f"binary may be missing — re-run: `{chromium_remedy}`[/]")
            return

        console.print(f"[yellow]{row['line']}[/]")
        if dry_run:
            console.print("plan (nothing will be installed):")
            console.print(walks_plan_description())
            return

        if not click.confirm(
                "Visual-proof walks require playwright and chromium "
                f"({WALKS_DOWNLOAD_SIZE}). Install now? [y/n]",
                default=False, show_default=False, prompt_suffix=""):
            console.print("aborted — nothing installed.")
            return

        from ..walks_provision import install_walks

        ok, messages = install_walks()
        for m in messages:
            colour = "green" if m.startswith("OK") else "red"
            console.print(f"  [{colour}]{m}[/]")
        if ok:
            console.print("[bold green]visual-proof walks installed[/]")
        else:
            console.print(
                "[bold red]install failed[/] — partial state may remain "
                "(no automatic rollback); re-run `nh doctor --fix-walks` "
                "to retry, it is safe to run again.")
            sys.exit(1)
        return

    from ..agent.backend_check import check_backend

    async def _go():
        async with Store(config.db_path) as store:
            d = await diagnose(store, config.data)
            tasks = (await store.query_one("SELECT COUNT(*) FROM tasks"))[0]

        # Live readiness (not history): can the coding backend actually run a
        # task right now? A missing `claude` CLI makes the board load green
        # while every task fails at launch — a contradiction, so it flips
        # `healthy` and the exit code below.
        llm = config.get("llm") or {}
        profile = llm.get("auth_profile")
        auth_mode = llm.get("auth_mode", "subscription")
        backend = check_backend(profile=profile, auth_mode=auth_mode)
        for reason in backend.reasons:
            d.contradictions.append(f"CODING BACKEND UNUSABLE: {reason}")

        # Codex row: computed unconditionally (codex need not be the
        # selected backend) — pure and read-only, see doctor.codex_row's
        # docstring. Run HERE, before the verdict is computed, so an invalid
        # llm.codex_auth_mode (a typo, on an install that may not even use
        # codex) is a contradiction like any other and the exit code follows
        # it — codex_row itself never raises for this (its own docstring's
        # rule: a diagnostic must never crash the command that prints it).
        from ..doctor import codex_row

        crow = codex_row(config.data)
        if crow.get("error"):
            d.contradictions.append(
                f"CODEX CONFIG INVALID: {crow['error']}"
            )

        # The gap presence-checking cannot close: a valid-SHAPED but expired or
        # revoked credential passes everything above and dies at the first task
        # (walkthrough B5). Opt-in, because the rule that doctor never spends
        # quota unasked is what makes the rest of it safe to run anywhere. Run
        # HERE, before the verdict is computed, so a rejected credential is a
        # contradiction like any other and the exit code follows it.
        auth_note = "presence only — no live auth call"
        if verify_auth:
            from ..agent.backend_check import verify_credential_live

            problem = await verify_credential_live(
                model=config.utility_model, profile=profile,
                auth_mode=auth_mode)
            if problem is None:
                auth_note = "verified by one live call"
            elif problem[0] == "inconclusive":
                # Transport failure judges the NETWORK, not the credential —
                # a cron doctor on a flaky link must not read as a dead
                # credential (independent-review finding). Not a
                # contradiction; the exit code is unchanged by this outcome.
                auth_note = "live call NOT VERIFIED (transport failure)"
                console.print(
                    f"    [yellow]! credential not verified —[/] {problem[1]}")
            else:
                auth_note = "live call REJECTED"
                d.contradictions.append(
                    f"CREDENTIAL DOES NOT WORK: a single live call was made "
                    f"with it and failed — {problem[1]}")

        # The verdict, first — a first run printed 149 lines of all-zero
        # mechanism rows and internal history, and a newcomer could not tell
        # healthy from broken. Three lines say it; the table is still one flag
        # away. This reads `d.healthy`, it does not redefine it: the predicate
        # and the exit code below are exactly what they were.
        fired = sum(1 for m in d.mechanisms if m["count"])
        if d.healthy:
            console.print("[bold green]install healthy[/] — no contradictions, "
                          "no evidence gaps")
        else:
            console.print(
                f"[bold red]install needs attention[/] — "
                f"{len(d.contradictions)} contradiction(s), "
                f"{len(d.evidence_gaps)} evidence gap(s) below")
        console.print(
            "[dim]nothing has run yet[/]" if not fired else
            f"{fired} of {len(d.mechanisms)} mechanisms have fired")
        console.print(f"{tasks} task(s)")
        console.print()

        # The two live facts about THIS install: which credential pays, and
        # whether the thing that spends it is even present. doctor loaded both
        # of these and printed neither — quickstart.md promised it "reports
        # your auth profile and mode" and nothing in 149 lines said "auth".
        cli = backend.cli_path or "not found"
        colour = "green" if backend.ready else "red"
        console.print(f"[bold]auth[/] — profile: "
                      f"[cyan]{profile or DEFAULT_AUTH_PROFILE}[/]  "
                      f"mode: [cyan]{auth_mode}[/]  "
                      f"[dim]({auth_note})[/]")
        console.print(f"[bold]coding backend[/] — claude CLI: "
                      f"[{colour}]{cli}[/]")

        # Codex row: `crow` was computed above, before the verdict, so an
        # invalid llm.codex_auth_mode already drove the exit code via the
        # contradiction appended there; this only decides how the row reads.
        if crow.get("error"):
            console.print(
                f"[bold]codex backend[/] — [red]CONFIG INVALID[/]: {crow['error']}"
            )
        else:
            codex_colour = "green" if crow["present"] else "yellow"
            console.print(
                f"[bold]codex backend[/] — mode: [cyan]{crow['mode']}[/]  "
                f"credential: [{codex_colour}]{'present' if crow['present'] else 'not found'}[/]  "
                f"model: [cyan]{crow['model']}[/]  "
                f"cli: [dim]{crow['cli_path']}[/]"
            )

        # The detailed readiness row only appears when the codex backend is
        # actually in play (worker.backend == "codex", or a live task asked
        # for --backend codex) — `d.codex` is `{"selected": False}` otherwise
        # and `codex_readiness` spawned nothing to produce it.
        if d.codex and d.codex.get("selected"):
            cx_colour = "green" if d.codex.get("flags_ok") else "red"
            cx_cli = d.codex.get("cli_path") or "not found"
            cx_version = d.codex.get("version")
            cx_version_note = f" ({cx_version})" if cx_version else ""
            console.print(f"[bold]coding backend[/] — codex CLI: "
                          f"[{cx_colour}]{cx_cli}[/]{cx_version_note}  "
                          f"approval: {d.codex.get('flag_detail') or 'UNSUPPORTED'}")
            key_colour = "green" if d.codex.get("api_key_present") else "red"
            key_state = "present" if d.codex.get("api_key_present") else "MISSING"
            cx_label = ("ChatGPT session" if d.codex.get("mode") == "subscription"
                        else "OPENAI_API_KEY")
            console.print(f"                {cx_label}: "
                          f"[{key_colour}]{key_state}[/]  [dim](presence only)[/]")
            console.print(f"                [dim]{d.codex.get('entitlement_note')}[/]")

        _print_visual_walks(console, d)

        if verbose:
            console.print("[bold]mechanism liveness[/] (lifetime firings)")
            for m in d.mechanisms:
                when = (datetime.fromtimestamp(m["last_ts"]).strftime("%Y-%m-%d %H:%M")
                        if m["last_ts"] else "never")
                colour = "green" if m["count"] else "yellow"
                line = f"  [{colour}]{m['name']:<18}[/] {m['count']:>6}  last: {when}"
                if m["hint"]:
                    line += f"  [dim]{m['hint']}[/]"
                console.print(line)
        else:
            console.print(
                f"[bold]mechanism liveness[/] — {fired}/{len(d.mechanisms)} have "
                f"ever fired  [dim](nh doctor --verbose for the table)[/]")

        if d.contradictions:
            console.print("\n[bold red]contradictions[/] — evidence of activity "
                          "without evidence of the mechanism:")
            for c in d.contradictions:
                console.print(f"  [red]✗[/] {c}")
        if d.evidence_gaps:
            console.print("\n[bold yellow]evidence gaps[/] — statuses not backed "
                          "by events:")
            for g in d.evidence_gaps:
                console.print(f"  [yellow]![/] {g}")
        if d.advisories:
            console.print("\n[bold cyan]advisories[/] — prunable leftovers "
                          "(do not affect health):")
            for a in d.advisories:
                console.print(f"  [cyan]•[/] {a}")
        # The healthy line is the verdict at the top — printed once, in the
        # same words ("no contradictions, no evidence gaps") anything grepping
        # this output already looks for, rather than twice.
        return d.healthy

    # The exit code is the machine-readable half of this command, and for a
    # long time it was a constant 0 — `nh doctor || exit 1` in a CI job or a
    # pre-flight script could never fire, so every gate reporting through
    # doctor was invisible to automation. `healthy` is the existing severity
    # line (contradictions + evidence gaps, never advisories); the exit code
    # simply follows it rather than introducing a second one. Output is
    # unchanged — anything parsing stdout keeps working.
    if not asyncio.run(_go()):
        sys.exit(1)


@cli.command("start")
@click.option("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
@click.option("--port", default=None, type=int, help="Bind port (default from config).")
@click.option("--workers", default=None, type=int,
              help="Max concurrent tasks (default 1 = serial mode).")
@click.option("--no-open", is_flag=True, help="Don't open browser.")
def start(host, port, workers, no_open):
    """Start no_human: web board + task worker. The only command you need.

    \b
    This single command starts:
      • The web board (FastAPI + React UI)
      • A task worker that picks up and runs new tasks
      • The wake watcher (auto-resumes blocked tasks)

    \b
    No configuration needed beyond `nh init`. Tasks created from the
    board are automatically picked up and run by the embedded worker.

    \b
    Examples:
      nh start                     # board + 1 serial worker
      nh start --workers 3         # board + 3 concurrent workers
      nh start --no-open           # don't open browser
    """
    config, _ = _bootstrap()
    _assert_backend_usable()
    _warn_if_editable_install_dangles()

    if not _acquire_pid_lock():
        console.print(
            "[red]another no_human instance is already running[/]\n"
            "Kill it first, or remove the stale lock:\n"
            "  [bold]rm ~/.no_human/nh.pid[/]"
        )
        sys.exit(1)

    port = port or config.data.get("server", {}).get("port", 8420)

    # Determine worker concurrency: explicit flag > config > 1 (serial), then
    # clamped to 1 unless concurrency.enabled. Separately clamped when
    # isolation.enabled is off — parallel tasks would then share one checkout.
    from ..core.scheduler import resolve_max_workers
    conc = config.data.get("concurrency", {})
    max_workers, worker_warning = resolve_max_workers(config.data, override=workers)
    if worker_warning:
        console.print(f"[yellow]⚠ {worker_warning}[/]")
    poll_interval = 10
    try:
        from ..blockers import parse_duration
        raw = str(conc.get("poll_interval", "10s"))
        interval = parse_duration(raw)
        if interval:
            poll_interval = int(interval.total_seconds())
    except Exception:  # noqa: BLE001
        pass

    url = f"http://{host}:{port}"
    mode = f"{max_workers} worker(s)" + (" · serial" if max_workers == 1 else " · concurrent")
    console.print(f"[bold green]no_human[/]  {url}  ·  {mode}")
    console.print("[dim]ctrl-c to stop[/]")

    if not no_open:
        import webbrowser
        webbrowser.open(url)

    # Placement is load-bearing: `_WEB_DIST` and the `StaticFiles` mount are
    # decided at `api/app` IMPORT time (module scope, not per-request), so a
    # rebuild triggered after that import below would never be picked up by
    # this running process.
    _ensure_board_fresh()

    import uvicorn
    from ..api.app import app as _app

    # CLI overrides for worker concurrency (lifespan reads these). Always set,
    # and always the *resolved* value, so the pool the server builds is exactly
    # the one we just announced — the two used to be computed independently.
    _app.state._worker_opts = {
        "max_workers": max_workers,
        "poll_interval": poll_interval,
    }

    # Build the server ourselves (instead of uvicorn.run) so we can run it in
    # the same event loop as the Jira poll loop below — mirrors `serve`'s
    # `await asyncio.gather(*coros)` shape without touching `serve` itself.
    server = uvicorn.Server(uvicorn.Config(_app, host=host, port=port, log_level="warning"))

    async def _go():
        # One aiosqlite connection for the whole `nh start` process. Jira and
        # Linear intake used to each open their OWN `Store(config.db_path)`
        # here — a second (and third) connection to the SAME file, started
        # concurrently with the app lifespan's own connect+migrate (fired
        # inside `server.serve()` below) and with no `busy_timeout` set, so
        # the loser of any write race failed immediately instead of waiting.
        # That is what flooded a clean startup with 216 lines of
        # `sqlite3.OperationalError: database is locked` — two CONNECTIONS in
        # one process, not two servers (`lsof` still shows a single pid).
        # `nh serve` already shares one `store` across the scheduler and both
        # intakes; this makes `start()` do the same, handing the connection to
        # the app via `app.state._external_store` so `lifespan` (api/app.py)
        # reuses it instead of opening its own.
        store = await Store(config.db_path).connect()
        _app.state._external_store = store

        # Jira intake (SCRUM-21): write-back parity with `nh serve` — same
        # opt-in flag, cadence parsing, and on_event print as the poller
        # block there (reused verbatim, not extracted into a shared helper).
        # A setup failure is caught so a misconfigured Jira integration never
        # breaks `nh start`: it's opt-in and must degrade gracefully.
        jira_task = None
        jira_stop = None
        jira_cfg = (config.data.get("integrations") or {}).get("jira") or {}
        if jira_cfg.get("enabled"):
            try:
                from ..config import load_env_var
                from ..intake.jira import JiraAdapter
                from ..intake.jira_poll import JiraPoller
                load_env_var("JIRA_API_TOKEN")  # from ~/.no_human/.env into the process env
                jira_secs = max(60, int((parse_duration(str(jira_cfg.get("poll_interval", "5m")))
                                         or parse_duration("5m")).total_seconds()))
                poller = JiraPoller(
                    JiraAdapter(config.data), store, config=config.data,
                    on_event=lambda k, t: console.print(f"[cyan]◆ {k}[/] {t}"))
                console.print(f"[green]Jira intake[/] project={jira_cfg.get('project_key') or '?'} "
                              f"poll={jira_secs}s")
                jira_stop = asyncio.Event()
                jira_task = asyncio.create_task(_jira_poll_loop(poller, jira_stop, jira_secs))
            except Exception as exc:  # noqa: BLE001 — optional integration, never break `start`
                console.print(f"[yellow]Jira intake failed to start[/] {exc}")
                jira_task = jira_stop = None

        # Linear intake: same shape, same opt-in discipline, sharing the same
        # store — its own stop event so neither tracker's failure can take the
        # other down.
        linear_task = None
        linear_stop = None
        linear_cfg = (config.data.get("integrations") or {}).get("linear") or {}
        if linear_cfg.get("enabled"):
            try:
                from ..config import load_env_var
                from ..intake.linear import LinearAdapter
                from ..intake.linear_poll import LinearPoller
                load_env_var("LINEAR_API_KEY")  # from ~/.no_human/.env into the process env
                linear_secs = max(60, int((parse_duration(str(linear_cfg.get("poll_interval", "5m")))
                                           or parse_duration("5m")).total_seconds()))
                linear_poller = LinearPoller(
                    LinearAdapter(config.data), store, config=config.data,
                    on_event=lambda k, t: console.print(f"[cyan]◆ {k}[/] {t}"))
                console.print(f"[green]Linear intake[/] team={linear_cfg.get('team_key') or '?'} "
                              f"poll={linear_secs}s")
                linear_stop = asyncio.Event()
                linear_task = asyncio.create_task(
                    _linear_poll_loop(linear_poller, linear_stop, linear_secs))
            except Exception as exc:  # noqa: BLE001 — optional integration, never break `start`
                console.print(f"[yellow]Linear intake failed to start[/] {exc}")
                linear_task = linear_stop = None

        # monday.com intake: same shape, same opt-in discipline, sharing the
        # same store — its own stop event so no tracker's failure can take
        # another down.
        monday_task = None
        monday_stop = None
        monday_cfg = (config.data.get("integrations") or {}).get("monday") or {}
        if monday_cfg.get("enabled"):
            try:
                from ..config import load_env_var
                from ..intake.monday import MondayAdapter
                from ..intake.monday_poll import MondayPoller
                load_env_var("MONDAY_API_TOKEN")  # from ~/.no_human/.env into the process env
                monday_secs = max(60, int((parse_duration(str(monday_cfg.get("poll_interval", "5m")))
                                           or parse_duration("5m")).total_seconds()))
                monday_poller = MondayPoller(
                    MondayAdapter(config.data), store, config=config.data,
                    on_event=lambda k, t: console.print(f"[cyan]◆ {k}[/] {t}"))
                console.print(
                    f"[green]monday intake[/] board={monday_cfg.get('board_id') or '?'} "
                    f"status_column={monday_cfg.get('status_column') or '?'} "
                    f"poll={monday_secs}s")
                monday_stop = asyncio.Event()
                monday_task = asyncio.create_task(
                    _monday_poll_loop(monday_poller, monday_stop, monday_secs))
            except Exception as exc:  # noqa: BLE001 — optional integration, never break `start`
                console.print(f"[yellow]monday intake failed to start[/] {exc}")
                monday_task = monday_stop = None

        try:
            await server.serve()
        finally:
            if jira_task is not None:
                jira_stop.set()
                try:
                    await asyncio.wait_for(jira_task, timeout=10)
                except asyncio.TimeoutError:
                    jira_task.cancel()
            if linear_task is not None:
                linear_stop.set()
                try:
                    await asyncio.wait_for(linear_task, timeout=10)
                except asyncio.TimeoutError:
                    linear_task.cancel()
            if monday_task is not None:
                monday_stop.set()
                try:
                    await asyncio.wait_for(monday_task, timeout=10)
                except asyncio.TimeoutError:
                    monday_task.cancel()
            # Ownership: this store was handed to the app via
            # `_external_store`; `lifespan` (api/app.py) skips closing it for
            # exactly this reason, so `start()` — the one that opened it — is
            # the one that closes it.
            await store.close()

    try:
        asyncio.run(_go())
    finally:
        _release_pid_lock()


@cli.command("dashboard")
@click.option("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
@click.option("--port", default=None, type=int, help="Bind port (default from config).")
@click.option("--no-open", is_flag=True, help="Don't open browser.")
def dashboard(host, port, no_open):
    """Alias for `nh start`. Starts board + worker."""
    # Forward to start() so there's only one code path.
    ctx = click.get_current_context()
    ctx.invoke(start, host=host, port=port, workers=None, no_open=no_open)


def _denied_message(pid: int) -> None:
    console.print(
        f"[red]pid {pid} is owned by another user[/] — not killing; "
        "remove ~/.no_human/nh.pid manually if stale"
    )


# `_kernel32` and `_windows_pid_alive` now live in ..config (next to pid_alive,
# which needs the same OpenProcess probe for the scheduler-lease dead-sibling
# check); imported above so there is one source of truth.


def _probe_pid(pid: int):
    """Is *pid* alive? True / False / None (another user's process).

    ``os.kill(pid, 0)`` is the POSIX idiom and is kept verbatim there. It CANNOT
    be used on Windows, and the mechanism is worse than the obvious guess: for
    most signals ``os.kill`` there calls ``TerminateProcess``, but signal 0 IS
    ``signal.CTRL_C_EVENT``, which ``os.kill`` implements as
    ``GenerateConsoleCtrlEvent`` — a Ctrl-C broadcast to a console process
    GROUP, the caller's own console included. So the liveness probe that guards
    the instance lock did not merely kill the running ``nh`` it was asked to
    detect — it could take down everything sharing the console, asynchronously,
    a moment later (measured: it killed the test suite exercising it, and the
    session driving that suite, three separate times before it was traced).
    """
    if _IS_WINDOWS:
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


# Escalation levels, named rather than passed as signal numbers: `signal.SIGKILL`
# does not exist on Windows and merely NAMING it is an AttributeError, which is
# how `nh stop` died there before it stopped anything.
_KILL_TERM = "term"
_KILL_FORCE = "force"


def _try_kill(pid: int, level: str = _KILL_TERM):
    """Ask *pid* to stop (``_KILL_TERM``) or force it (``_KILL_FORCE``).

    Returns True if the request was delivered, False if the process was already
    gone, or None if the pid is owned by another user — the caller must treat
    None as a hard stop and print nothing further.

    On Windows this is ``taskkill`` (with ``/T`` for the process tree), because
    there is no signal to send: POSIX SIGTERM has no Windows equivalent for a
    non-console child, and SIGKILL has no equivalent at all. UNTESTED ON
    WINDOWS.
    """
    if _IS_WINDOWS:
        return _windows_try_kill(pid, force=level == _KILL_FORCE)
    sig = signal.SIGKILL if level == _KILL_FORCE else signal.SIGTERM
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        _denied_message(pid)
        return None


def _windows_try_kill(pid: int, *, force: bool):
    """``taskkill`` the pid (and its tree). See :func:`_try_kill`."""
    import subprocess

    argv = ["taskkill", *(["/F"] if force else []), "/T", "/PID", str(pid)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except OSError:
        return None
    out = f"{proc.stdout or ''}{proc.stderr or ''}".lower()
    if proc.returncode == 0:
        return True
    if "not found" in out or proc.returncode == 128:
        return False
    if "denied" in out:
        _denied_message(pid)
        return None
    # Unknown failure: report it as delivered so the caller's wait/escalate
    # path decides, rather than claiming the process is gone.
    return True


def _wait_for_exit(pid: int, timeout: float):
    """Poll :func:`_probe_pid` until the process is gone or timeout elapses.

    Always checks at least once before consulting the clock, so timeout=0
    still confirms a process that already exited by the time this is
    called. Returns True once gone, False if still alive at the deadline,
    or None if the pid is owned by another user."""
    deadline = time.monotonic() + timeout
    while True:
        state = _probe_pid(pid)
        if state is False:
            return True
        if state is None:
            _denied_message(pid)
            return None
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _stop_server(timeout: float) -> int:
    """SIGTERM the pid in the pidfile, wait, escalate to SIGKILL on timeout.

    Only ever signals the pid read from the pidfile — never a guessed or
    discovered pid. Since pids are recycled by the OS, a stale pidfile can
    in principle point at an unrelated process (no cmdline check is done to
    rule this out); the liveness/permission checks below are the only
    guards. Returns the process exit code (0 success, 1 error). The pidfile
    is removed only once the target process is confirmed gone (or proven
    corrupt/stale) — never while it may still be alive.
    """
    from ..config import NO_HUMAN_HOME
    lock_path = NO_HUMAN_HOME / "nh.pid"

    if not lock_path.exists():
        console.print("[yellow]no_human is not running[/] (no ~/.no_human/nh.pid)")
        return 1

    try:
        pid = int(lock_path.read_text().strip())
    except ValueError:
        console.print("[yellow]stale pidfile[/] (unreadable) — cleaning up")
        lock_path.unlink(missing_ok=True)
        return 1

    if pid <= 1 or pid == os.getpid():
        console.print(f"[red]corrupt pidfile[/] — refusing to signal pid {pid}; cleaning up")
        lock_path.unlink(missing_ok=True)
        return 1

    state = _probe_pid(pid)
    if state is False:
        console.print(f"[yellow]stale pidfile[/] — pid {pid} not running; cleaning up")
        lock_path.unlink(missing_ok=True)
        return 1
    if state is None:
        _denied_message(pid)
        return 1

    result = _try_kill(pid, _KILL_TERM)
    if result is None:
        return 1
    if result is False:
        lock_path.unlink(missing_ok=True)
        console.print(f"[green]✓ stopped[/] (pid {pid})")
        return 0

    gone = _wait_for_exit(pid, timeout)
    if gone is None:
        return 1
    if gone:
        lock_path.unlink(missing_ok=True)
        console.print(f"[green]✓ stopped[/] (pid {pid})")
        return 0

    # Wedged: the graceful stop didn't take effect within the bound — escalate.
    result = _try_kill(pid, _KILL_FORCE)
    if result is None:
        return 1
    if result is False:
        lock_path.unlink(missing_ok=True)
        console.print(f"[green]✓ stopped[/] (pid {pid})")
        return 0

    gone = _wait_for_exit(pid, timeout)
    if gone is None:
        return 1
    if gone:
        lock_path.unlink(missing_ok=True)
        console.print(f"[yellow]force-killed[/] (pid {pid} did not respond to SIGTERM)")
        return 0

    console.print(
        f"[red]still running[/] — pid {pid} survived SIGKILL; pidfile left in place"
    )
    return 1


def _default_stop_timeout(config_data: dict | None) -> float:
    """`nh stop --timeout` when none is given: the server's stop grace plus
    a margin, read from the same config the server reads, so SIGKILL lands
    only after the lifespan's own wait (grace + its smaller margin) has ended."""
    from ..core.scheduler import STOP_COMMAND_MARGIN_S, stop_grace_s
    return stop_grace_s(config_data) + STOP_COMMAND_MARGIN_S


@cli.command("stop")
@click.option("--timeout", default=None, type=float,
              help="Seconds to wait after SIGTERM before escalating to SIGKILL "
                   "(default: concurrency.stop_grace_s + 15, i.e. 75).")
def stop(timeout):
    """Stop the running `nh start`/`nh serve` server.

    Reads the pid from ~/.no_human/nh.pid, sends SIGTERM, waits up to
    --timeout seconds, then escalates to SIGKILL if the process is wedged.
    Pairs with `nh start` — this is the command referenced by the
    auth-switch restart hint.

    What SIGTERM does: the scheduler asks every running attempt to checkpoint
    — the coder session unwinds at its next tool boundary, uncommitted work is
    committed as [WIP-PARTIAL], `resume_from` is stamped, the attempt row is
    closed as interrupted (its tokens count, it does not consume a lifetime
    attempt) and the task stays claimable — then drains for
    `concurrency.stop_grace_s` (60 s). The next `nh start` resumes those
    tasks from their checkpoint. What the grace cannot reach: a coder session
    inside ONE long tool call (a full test suite), and the planner / reviewer
    sessions, which are not interruptible — past the grace the process exits
    as before and that task resumes from its last commit, not its edits.

    The default timeout is therefore derived, not a literal: grace + 15 s,
    so the kill cannot land before the drain it is waiting for. Pass
    --timeout to choose your own; anything below the grace defeats the
    checkpoint.
    """
    if timeout is None:
        try:
            from ..config import load_config
            # A read, not a bootstrap: a stop must not create a config file.
            timeout = _default_stop_timeout(
                load_config(create_if_missing=False).data)
        except Exception:  # noqa: BLE001 — an unreadable/absent config must not block a stop
            timeout = _default_stop_timeout(None)
    sys.exit(_stop_server(timeout))


# --------------------------------------------------------------------------- #
# Evaluation harness (PLAN.md Part 21)                                        #
# --------------------------------------------------------------------------- #

@cli.command("eval")
@click.option("--prev", "prev_path", default=None, type=click.Path(),
              help="Previous scorecard JSON to diff against (CI gate).")
@click.option("--out", "out_path", default=None, type=click.Path(),
              help="Write the scorecard JSON here.")
@click.option("--gate", is_flag=True, help="Exit non-zero if the CI gate fails.")
def eval_cmd(prev_path, out_path, gate):
    """Replay the golden task set and emit a scorecard (Part 21).

    Runs on subscription auth via the real backend; a deliberately-impossible
    golden task must be escalated, never faked.
    """
    config, _ = _bootstrap()
    from ..agent.claude_backend import ClaudeBackend
    from ..eval import Scorecard, render_scorecard, run_eval
    from ..eval.judge import IntentJudge
    from ..review.reviewer import AdversarialReviewer

    def backend_factory(_golden):
        return ClaudeBackend(
            model=config.primary_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
            never_push_to=config["git"]["never_push_to"],
        )

    async def _go():
        previous = Scorecard.load(Path(prev_path)) if prev_path else None
        run = await run_eval(
            config.data,
            backend_factory=backend_factory,
            reviewer=AdversarialReviewer.from_config(config.data),
            judge=IntentJudge(model=config.review_model),
            previous=previous,
            now=_now_iso(),
            on_event=lambda e: console.print(
                f"[dim]· {e.get('kind')}: {e.get('task', '')}"
                f"{' ✓' if e.get('correct') else ''}[/]"),
        )
        console.print(render_scorecard(run.scorecard, previous))
        if out_path:
            run.scorecard.save(Path(out_path))
            console.print(f"[dim]scorecard → {out_path}[/]")
        if not run.gate.passed:
            console.print("[bold red]CI gate FAILED:[/]")
            for r in run.gate.reasons:
                console.print(f"  ⛔ {escape(str(r))}")
            if gate:
                sys.exit(1)
        else:
            console.print("[bold green]CI gate passed[/]")

    asyncio.run(_go())


@cli.group("bench")
def bench():
    """North-star benchmark: replay the operator's REAL historical tasks.

    \b
    build  → specs from conversation history (no-cheat: initial request only);
    run    → replay through the real pipeline in push-proof sandboxes,
             recording <label>-<stamp>.json — publishing nothing;
    publish→ promote one results file to the baseline + the committed report;
    compare→ PAIR two results files spec by spec (flips both ways, McNemar on
             the discordant pairs, flaky-canary over a longer history) —
             a report only: it writes nothing and never exits non-zero;
    report → re-render docs/NORTH_STAR_BENCH.md from results/latest.json, the
             LATEST SAVED RESULTS — NOT the published baseline, which is a
             separate file only a clean publish writes.
    """


def _slug(label: str) -> str:
    """Filename-safe form of a run label. A label reaches this from the command
    line and becomes a path, so anything that could traverse or collide is
    flattened rather than trusted."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (label or "run").strip()).strip("-.")
    return slug or "run"


def _spec_set_key(specs, trials: int = 1) -> str:
    """Short stable digest of which specs a run covers.

    Two runs with different spec sets must never share a checkpoint, whatever
    they are labelled: an unlabelled `--limit 1` probe and an unlabelled
    `--full` run both slug to "run", and the probe's clean completion deleted
    the full run's checkpoint. Keying on the spec set is what separates them.

    `trials` joins the key for the same reason — a 3-trial run and a 1-trial
    run over the SAME specs are different work, and a 1-trial run resuming the
    3-trial checkpoint would drop two thirds of it on the floor and then unlink
    it as "completed". It is folded in only when > 1 so every checkpoint
    written before trials existed keeps its filename and stays resumable.
    """
    import hashlib
    ids = ",".join(sorted(s.id for s in specs))
    if trials > 1:
        ids += f"|trials={trials}"
    return hashlib.sha256(ids.encode()).hexdigest()[:8]


@bench.command("build")
@click.option("--days", default=400, help="History horizon.")
@click.option("--roots", multiple=True, type=click.Path(path_type=Path),
              help="Claude Code projects roots (default: both config dirs).")
@click.option("--out", "out_dir", default=None, type=click.Path(path_type=Path),
              help="Spec output dir (default: eval/northstar_tasks/generated — "
                   "GITIGNORED; raw specs hold verbatim conversation content).")
def bench_build(days, roots, out_dir):
    """Build benchmark specs from ALL conversation sources."""
    from ..eval.bench_task import (GENERATED_DIR, build_bench_tasks,
                                   load_bench_tasks, spec_pin_not_rederivable,
                                   spec_pin_rederived)
    from ..history.claude_code import extract_claude_code_transcripts
    from ..history.extractor import IDENotRunningError, extract_transcripts

    transcripts = []
    try:
        transcripts += extract_transcripts(days=days)
    except (IDENotRunningError, ImportError) as exc:
        console.print(f"[dim]windsurf: skipped ({escape(str(exc))})[/]")  # term-ok: real IDE names
    # min_user_msgs=1: a one-shot request is a real task for the bench corpus.
    transcripts += extract_claude_code_transcripts(
        days=days, limit=10_000, roots=list(roots) or None, min_user_msgs=1)

    target = Path(out_dir) if out_dir else GENERATED_DIR
    written = build_bench_tasks(transcripts, out_dir=target)
    runnable = sum(1 for _ in written)  # count; runnable split shown by report
    # Reloaded rather than counted off `written` (a list of paths, not specs):
    # a history rewrite can leave a spec's recorded pin unreachable, and
    # disclosure of that repair is mandatory here — a rebuild that silently
    # re-derived pins must never look identical to a clean one.
    _rebuilt = load_bench_tasks(target)
    rederived = sum(1 for s in _rebuilt if spec_pin_rederived(s))
    not_rederivable = sum(1 for s in _rebuilt if spec_pin_not_rederivable(s))
    # int(...) wrapping: not a type-safety concern (both are already ints from
    # sum()) but the AST guard (tests/_bench_ast_guard.py) only recognizes
    # Constant/len/int/float/round/sum/abs/escape calls/JoinedStr/BinOp as
    # provably safe by shape — a bare Name would need a per-name allowlist
    # entry in tests/test_bench_print_escape.py instead, which is exactly the
    # enumeration that guard's docstring says it replaced.
    console.print(f"[green]{runnable} specs written[/] · "
                 f"{int(rederived)} pin(s) re-derived · "
                 f"{int(not_rederivable)} not re-derivable → {escape(str(target))}")
    console.print("[dim]curate the core subset by copying reviewed specs up "
                  "into eval/northstar_tasks/ and setting subset: core[/]")


def _bench_cost_cell(cost_ratio: float | None, basis: str) -> str:
    """The per-spec cost cell in `nh bench run`'s live output.

    Named so the rule is testable: it lives inside a long async run loop and
    could not otherwise be exercised without a full bench run.

    Falsy, not `is not None`. `cost_ratio` is 0.0 when no_human spent nothing —
    crashed, skipped, or escalated before any model call — and this cell printed
    `cost×0.00` for it, the best possible cost result, next to a ❌. The
    aggregate already refuses that reading: `northstar_card.priced_scores`
    excludes both None and 0.0 because "a 0.0 is a non-result, not a cost win".
    The median was therefore honest while the rows above it were not.

    Deliberately NOT fixed in `BenchScore.cost_ratio`: nh/orig genuinely IS 0.0
    there, and `tests/test_northstar_card.py` pins that. The judgement about
    what a 0.0 MEANS belongs to each consumer, and this was the consumer that
    was not making it.

    `basis` is REQUIRED, not optional: a cost ratio can only be rendered
    alongside the price table it was computed against (`BenchScore.
    cost_ratio_basis`) — see northstar bench cost ratio part 2. There is no
    default that would let a call site print the number without it.
    """
    return f"cost×{cost_ratio:.2f} ({basis})" if cost_ratio else "cost n/a"


@bench.command("harvest")
@click.option("--out", "out_dir", default=None, type=click.Path(path_type=Path),
              help="Candidate output dir (default: ~/.no_human/harvest — "
                   "OUTSIDE the corpus; candidates are runnable:false until "
                   "the operator curates them in).")
def bench_harvest(out_dir):
    """Turn escalated/parked/failed tasks into bench-spec CANDIDATES.

    Every terminal non-success is a replayable scenario for exactly the
    failure modes the bench measures. Candidates are written runnable:false
    with a harvest: provenance block; nothing enters the scored corpus until
    the operator pins the repo state, sets the subset, and judges
    expect_escalation (the corpus feeds a published trust number — nothing
    enters it un-reviewed).
    """
    config = load_config()

    async def _run():
        from ..eval.harvest import harvest
        async with Store(config.db_path) as store:
            return await harvest(store, out_dir=out_dir)

    written = asyncio.run(_run())
    if written:
        console.print(f"[green]{len(written)} candidate(s) written[/] → "
                      f"{escape(str(written[0].parent))}")
        console.print("[dim]curate: pin repo.pin, set subset, judge "
                      "expect_escalation, then move into eval/northstar_tasks/[/]")
    else:
        console.print("[dim]no new candidates (existing files are never "
                      "overwritten)[/]")


@bench.command("run")
@click.option("--full", is_flag=True,
              help="Run the FULL corpus (default: subset core only).")
@click.option("--limit", default=0, help="Cap the number of tasks (0 = no cap).")
@click.option("--gate", is_flag=True,
              help="Exit non-zero on regression, OR when the run did not measure "
                   "enough of the corpus (too much skipped/dead, a filtered or "
                   "capped slice, or narrower than the baseline). NOTE: the "
                   "available-spec count is read from the canonical corpus "
                   "regardless of --specs-dir, so a deliberately small scratch "
                   "corpus cannot pass --gate. That is intended: a filtered "
                   "slice must not be able to stand for the corpus.")
@click.option("--prev", "prev_path", default=None, type=click.Path(),
              help="Previous latest.json (default: eval/results/northstar/latest.json).")
@click.option("--label", default="", help="Label for this run (e.g. the change).")
@click.option("--resume", is_flag=True,
              help="Skip specs already scored in the checkpoint (progress.json) "
                   "— continue a run that died on quota saturation.")
@click.option("--specs-dir", default=None, type=click.Path(path_type=Path),
              help="Read specs from here too (default: eval/northstar_tasks + generated/ when --full).")
@click.option("--parallel", default=1, type=click.IntRange(1, 16),
              help="Run up to N specs concurrently (each spec is already "
                   "sandbox-isolated, so this is a pure wall-clock win). "
                   "Values above ~4 risk saturating the shared subscription "
                   "quota mid-run; default 1 = today's serial behavior.")
@click.option("--quick", is_flag=True,
              help="Stratified iteration tier: ONE representative per coverage "
                   "cell (project × runnable × expect-escalation × size), "
                   "picked deterministically — the corpus's whole variety at a "
                   "fraction of the wall clock. Iteration signal only; a quick "
                   "card cannot publish as the baseline.")
@click.option("--trials", default=1, type=click.IntRange(1, 20),
              help="Replay EACH spec N times and record every trial. Default 1 "
                   "= today's single-run behaviour. N>1 buys the two things a "
                   "single run cannot give: a narrower confidence interval, "
                   "and pass^N — the share of specs that pass EVERY trial, "
                   "which is what separates a capability from a coin flip. "
                   "Costs N× the wall clock and N× the tokens.")
def bench_run(full, limit, gate, prev_path, label, specs_dir, resume, parallel,
              quick, trials):
    """Replay bench specs through the REAL pipeline; score vs the originals."""
    import tempfile

    if quick and full:
        raise click.UsageError(
            "--quick and --full are mutually exclusive: --quick is a "
            "stratified slice of the core subset; --full is the whole corpus.")

    from ..agent.claude_backend import ClaudeBackend
    from ..eval.bench_task import GENERATED_DIR, NORTHSTAR_DIR, load_bench_tasks
    from ..eval.judge import GoalJudge
    from ..eval.northstar import NorthStarRunner
    from ..eval.northstar_card import (
        RESULTS_DIR,
        NorthStarCard,
        northstar_gate,
        pin_rederivation_note,
        publish_refusals,
        success_headline,
    )
    from ..eval.quota_halt import (
        QUOTA_HALT_CONSECUTIVE_DEAD,
        QuotaHaltDetector,
        resume_command,
    )
    from ..review.reviewer import AdversarialReviewer

    try:
        if specs_dir:
            specs = load_bench_tasks(Path(specs_dir))
        else:
            specs = load_bench_tasks(NORTHSTAR_DIR, subset=None if full else "core")
            if full:
                specs += load_bench_tasks(GENERATED_DIR)
    except ValueError as exc:
        # A malformed repo map raises with a precise, human-readable message.
        # Unhandled it exited 1 with a traceback and NO console output, so the
        # one thing that would tell the operator what to fix never rendered.
        console.print(f"[red]{escape(str(exc))}[/]")
        sys.exit(1)
    seen: set[str] = set()
    specs = [s for s in specs if not (s.id in seen or seen.add(s.id))]
    # How much corpus EXISTS, measured before --limit and independently of
    # --specs-dir. Coverage is a ratio over what a run LOADED, so filtering to
    # the specs that still resolve reads as perfect coverage; comparing loaded
    # against available is the only check that survives that.
    #
    # The CARD always records the FULL canonical corpus count — that is what
    # `publish_refusals` grades coverage with, and it is precisely what keeps
    # a quick card structurally unpublishable as the baseline (review finding,
    # 2026-07-25: writing the tier size here made a fresh-clone quick run
    # publish clean and poison every later full-run comparison). The tier's
    # own expected size is a RUNTIME-ONLY denominator, passed to the gate
    # call below and never stored.
    corpus_available = len(load_bench_tasks(NORTHSTAR_DIR, subset="core"))
    tier_expected = 0
    if quick:
        from ..eval.bench_task import select_quick_subset
        # From the CANONICAL corpus, same as corpus_available, so a smaller
        # --specs-dir cannot lower the bar the run is graded against.
        tier_expected = len(select_quick_subset(
            load_bench_tasks(NORTHSTAR_DIR, subset="core")))
        loaded = len(specs)
        specs = select_quick_subset(specs)
        console.print(
            f"[yellow]quick tier[/]: {len(specs)}/{loaded} specs — one per "
            "coverage cell; iteration signal only, not publishable as the "
            "baseline")
    if limit:
        specs = specs[:limit]
    # Pre-flight the corpus BEFORE spending a night on it. The repo map is
    # gitignored, so it is absent in every git worktree — without this, a run
    # launched from one silently fails to resolve most specs and reads as a
    # model regression rather than an operational accident.
    from ..eval.bench_task import (
        REPO_MAP_PATH,
        check_repo_map,
        redact_local_path,
        spec_project_name,
    )
    unresolved = check_repo_map(specs)
    if unresolved:
        console.print(
            f"[yellow]{len(unresolved)}/{len(specs)} spec repo(s) will not "
            f"resolve on this machine[/]")
        for line in unresolved[:10]:
            console.print(f"  ⚠ {escape(line)}")
        if len(unresolved) > 10:
            console.print(f"  [dim]… and {len(unresolved) - 10} more[/]")
        if not REPO_MAP_PATH.exists():
            console.print(
                f"[dim]no repo map at {escape(str(REPO_MAP_PATH))} — see "
                f"eval/repo_map.example.yaml[/]")
    if not specs:
        console.print("[yellow]no specs found — run `nh bench build` and curate "
                      "a core subset first[/]")
        sys.exit(1)

    config, _ = _bootstrap()

    def backend_factory(_spec):
        return ClaudeBackend(
            model=config.primary_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
            never_push_to=config["git"]["never_push_to"],
        )

    async def _go():
        def make_runner() -> NorthStarRunner:
            # A fresh runner (with its own reviewer/judge) per spec, mirroring
            # the serve pool's fresh-orchestrator-per-task pattern, so
            # concurrent specs never share mutable review state.
            return NorthStarRunner(
                config.data,
                backend_factory=backend_factory,
                reviewer=AdversarialReviewer.from_config(config.data),
                goal_judge=GoalJudge(model=config.review_model),
                event_sink=lambda e: None,
            )
        import json as _json
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        # Checkpoint keyed by label AND spec set. A shared progress.json meant a
        # one-spec probe and a 141-spec run collided: the probe "completed
        # cleanly" and its unlink() deleted the long run's only resumable state.
        # The label alone does not fix that — both default to "" — so the spec
        # set is what actually separates a probe from the corpus.
        ckpt = (RESULTS_DIR
                / f"progress-{_slug(label)}-{_spec_set_key(specs, trials)}.json")
        legacy_ckpt = RESULTS_DIR / "progress.json"
        if resume and not ckpt.exists() and legacy_ckpt.exists():
            # A run started before per-label checkpoints must stay resumable —
            # but ONLY if that checkpoint is this run's. Adopting it whenever it
            # existed re-created the original incident on the resume path: a
            # one-spec probe adopted a 56-spec checkpoint, filtered every spec
            # out as foreign, and then unlinked it on "clean completion". The
            # foreign-spec filter below proves the code can already tell; it
            # must refuse the file rather than inherit and delete it.
            legacy = NorthStarCard.load(legacy_ckpt)
            legacy_ids = {s.task_id for s in legacy.scores} if legacy else set()
            # Ownership, by the identity the checkpoint actually records. A
            # subset test is not ownership: a run whose spec set CONTAINS the
            # legacy specs passes it, which let `--full --resume` swallow the
            # 10 specs (8 of them dead) of an unrelated core run. An unlabelled
            # checkpoint is not identifiable at all, so it is declined rather
            # than guessed at — losing a one-time migration beats adopting
            # someone else's scores.
            owned = bool(legacy_ids) and bool(legacy.label) \
                and legacy.label == label and legacy_ids <= {s.id for s in specs}
            if owned:
                # COPY, never consume. The keyed file is this run's to rewrite
                # and unlink; `progress.json` is left in place forever, so no
                # code path can delete a checkpoint it did not create — which
                # is the whole incident, twice over.
                import shutil
                shutil.copy2(legacy_ckpt, ckpt)
            elif legacy_ids:
                console.print(
                    f"[yellow]not resuming from {escape(legacy_ckpt.name)}: it holds "
                    f"{len(legacy_ids)} spec(s) from run "
                    f"'{escape(legacy.label or 'unlabelled')}' — left untouched[/]")
        if resume and not ckpt.exists():
            # `--trials` is part of the checkpoint key (see `_spec_set_key`), so
            # changing it re-points --resume at a file that does not exist and
            # the run starts from zero — silently, while the banked work sits
            # on disk one filename away. Not adopted: those rows have a
            # different denominator and folding them in is the double-count
            # this key exists to prevent. But an operator about to re-pay for
            # completed trials is entitled to know the bill is a flag change,
            # not a lost checkpoint.
            spec_ids = {s.id for s in specs}
            stranded = 0
            for other in RESULTS_DIR.glob(f"progress-{_slug(label)}-*.json"):
                if other == ckpt:
                    continue
                oc = NorthStarCard.load(other)
                if oc is None:
                    continue
                stranded += sum(1 for s in oc.scores if s.task_id in spec_ids)
            if stranded:
                console.print(
                    f"[yellow]resume: {stranded} banked result(s) under a "
                    f"different --trials are not resumed; re-running[/]")
        scores = []
        done_keys: set = set()
        if resume and ckpt.exists():
            # Reload the partial run so a quota death doesn't waste completed
            # tasks (the expanded run died at 3/14 on "Stream closed"). Only
            # carry forward specs that belong to THIS run's set — a checkpoint
            # from a run with different --full/--limit/--specs-dir must not
            # bleed foreign specs into latest.json (the gate baseline).
            prev_card = NorthStarCard.load(ckpt)
            if prev_card is not None:
                spec_ids = {s.id for s in specs}
                # (task_id, trial) is the unit of work, so it is the unit a
                # resume carries forward. Two rules keep a trials run from
                # double-counting itself:
                #   - a trial index at or beyond this run's --trials is FOREIGN
                #     (resuming a 3-trial checkpoint with --trials 1 must not
                #     import trials 1 and 2 into a card that claims one), and
                #   - a (task_id, trial) already seen is dropped, so a
                #     checkpoint that somehow holds a duplicate contributes one
                #     score and not two. Without this the pass count for a spec
                #     can exceed the trial count it is divided by.
                scores = []
                # Collected as a LIST, not derived as a count afterwards: a
                # dropped duplicate is invisible to any after-the-fact key
                # comparison (its key is in the kept set), and it is exactly
                # the row an operator needs told about.
                foreign = []
                for sc in prev_card.scores:
                    key = (sc.task_id, sc.trial)
                    if (sc.task_id not in spec_ids or sc.trial >= trials
                            or key in done_keys):
                        foreign.append(sc)
                        continue
                    done_keys.add(key)
                    scores.append(sc)
                if foreign:
                    console.print(
                        f"[yellow]resume: ignoring {len(foreign)} checkpointed "
                        "result(s) not in this run — the spec set or trial "
                        "count changed; check your "
                        "--full/--limit/--specs-dir/--trials flags[/]")
                # The single-trial line is UNCHANGED, deliberately: it is what
                # an operator greps for and what the existing resume test
                # asserts, and "1 trial(s) across 1 spec(s)" says nothing extra
                # when there is only ever one trial per spec.
                if trials > 1:
                    console.print(
                        f"[green]resuming — {len(done_keys)} trial(s) already "
                        f"scored across {len({k[0] for k in done_keys})} "
                        f"spec(s)[/]")
                else:
                    console.print(f"[green]resuming — {len(done_keys)} spec(s) "
                                  "already scored[/]")
        base_tmp = Path(tempfile.mkdtemp(prefix="nh-bench-"))
        # Bounded pool. Each spec already runs in its own sandbox clone +
        # workdir, so --parallel is a pure wall-clock lever; at the default of
        # 1 the semaphore serializes in submission order — exactly the old
        # serial loop. The checkpoint lock keeps per-completion saves atomic.
        pool = asyncio.Semaphore(parallel)
        ckpt_lock = asyncio.Lock()
        halt = QuotaHaltDetector()

        async def _run_spec(spec, trial: int = 0):
            async with pool:
                if halt.stopped:
                    return
                # Built outside the print: rich reads a bracketed span as
                # markup, and an interpolated conditional is not a shape the
                # print guard can prove safe. escape() covers both.
                trial_bit = f"trial {trial + 1}/{trials} " if trials > 1 else ""
                console.print(
                    f"[dim]· {escape(spec.id)} {escape(trial_bit)}"
                    f"{escape(spec.title[:60])}[/]")
                # Trial 0 keeps the historical path so single-trial runs are
                # byte-identical to today; later trials MUST get their own
                # workdir or two concurrent trials of one spec share a sandbox
                # clone and a bench.db.
                # Concatenated, not f-string-interpolated: the escaping guard
                # (tests/test_bench_print_escape.py) reads this region by LINE,
                # so any line that interpolates the spec id into an f-string
                # reads to it as a print that forgot to escape. Bluntness is
                # the right call there — a path built in a print's shape is one
                # refactor away from being one — so this stays out of that
                # shape rather than arguing with the guard.
                wd = base_tmp / (spec.id + ("" if trial == 0
                                            else "-t" + str(trial)))
                wd.mkdir(parents=True, exist_ok=True)
                try:
                    score = await make_runner().run_one(spec, workdir=wd)
                except Exception as exc:  # noqa: BLE001 — one task's hard crash
                    # (e.g. the SDK CLI dying on quota saturation: "Stream
                    # closed") must not lose the run's partial results. Recorded
                    # honestly as crashed, never as satisfied.
                    from ..eval.northstar import BenchScore
                    # Redact BEFORE truncating. A CalledProcessError stringifies
                    # its whole argv, so an unresolvable repo puts the real
                    # local path into a note that is rendered into the TRACKED
                    # report — and whether an org name survives the 80-char cell
                    # depends only on how long the operator's home directory is.
                    # Computed once and used for both the record and the
                    # console line.
                    crash_note = redact_local_path(str(exc), spec)
                    orig = spec.original or {}
                    toks = orig.get("tokens", {}) or {}
                    score = BenchScore(
                        task_id=spec.id, title=spec.title,
                        outcome_status="crashed", goal_satisfied=False,
                        escalated_honestly=False, mergeable=None,
                        nh_tokens=0, nh_cache_tokens=0,
                        nh_cache_creation_tokens=0, nh_turns=0,
                        nh_wall_clock_s=0.0,
                        orig_tokens=int(toks.get("input_tokens", 0))
                        + int(toks.get("output_tokens", 0)),
                        orig_cache_tokens=int(toks.get("cache_read_input_tokens", 0)),
                        orig_cache_creation_tokens=int(
                            toks.get("cache_creation_input_tokens", 0)),
                        orig_wall_clock_s=float(orig.get("wall_clock_s", 0.0)),
                        orig_corrections=int(orig.get("corrections", 0)),
                        subset=spec.subset,
                        project=spec_project_name(spec),
                        notes=f"runner crashed: {crash_note[:300]}",
                    )
                    console.print(f"  [red]💥 {escape(spec.id)} crashed[/] "
                                  f"({escape(crash_note[:80])})")
                else:
                    mark = {True: "✅", False: "❌", None: "⏭"}[score.goal_satisfied]
                    ratio = _bench_cost_cell(score.cost_ratio, score.cost_ratio_basis)
                    console.print(
                        f"  {mark} {escape(spec.id)} {score.outcome_status} ({ratio})")
                # Stamped HERE, not inside the runner: `run_one`'s signature is
                # the seam every stubbed runner in the tests implements, and a
                # new required argument there would break them all while adding
                # nothing — the runner replays a spec, it does not know or care
                # which repetition it is.
                score.trial = trial
                # Checkpoint after EVERY completion so a mid-run death (quota
                # "Stream closed") never wastes the completed tasks — resume
                # with --resume.
                async with ckpt_lock:
                    scores.append(score)
                    halt.observe(score)
                    NorthStarCard(scores=halt.scored(scores),
                                  created_at=_now_iso(),
                                  corpus_available=corpus_available,
                                  trials=trials,
                                  label=label).save(ckpt)

        # Spec-major: every trial of one spec, then the next spec. Under
        # --parallel the pool interleaves them anyway; serially this keeps a
        # spec's repeats adjacent in the log, which is where a flip is read.
        await asyncio.gather(
            *(_run_spec(s, t) for s in specs for t in range(trials)
              if (s.id, t) not in done_keys))

        card = NorthStarCard(scores=halt.scored(scores), created_at=_now_iso(),
                             label=label, corpus_available=corpus_available,
                             trials=trials, halted_reason=halt.reason)
        prev_file = Path(prev_path) if prev_path else RESULTS_DIR / "latest.json"
        previous = NorthStarCard.load(prev_file)
        result = northstar_gate(card, previous, tier_expected=tier_expected)

        # A run RECORDS; it does not publish. Writing latest.json and the
        # committed report as a side effect of finishing is what let a saturated
        # run and a one-spec probe each overwrite the baseline. The result file
        # is immutable and named for its run, so nothing can collide with it.
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _now_iso().replace(":", "").replace("-", "")[:15]
        out = RESULTS_DIR / f"{_slug(label)}-{stamp}.json"
        # Second-resolution stamps collide for runs started in the same second.
        # A results file is the only record a run leaves, so it never overwrites
        # another one.
        n = 2
        while out.exists():
            out = RESULTS_DIR / f"{_slug(label)}-{stamp}-{n}.json"
            n += 1
        card.save(out)
        halt.keep_or_clear(ckpt)   # survives a quota halt; unlinked on clean completion
        agg = card.as_dict()["aggregate"]
        console.print(
            # The interval travels with the number everywhere it is printed.
            # A bare "success 91%" in a terminal is what gets pasted into a
            # README, and this run's own card is the only place that knows the
            # interval — so the console must not be the surface that drops it.
            f"[bold]success {escape(success_headline(card))}[/] · "
            f"median cost ratio {agg['median_cost_ratio']} (basis: {agg['median_cost_ratio_basis']}) · "
            f"corrections avoided {agg['corrections_avoided']} → "
            f"{out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")
        console.print(escape(pin_rederivation_note(card)))
        refusals = publish_refusals(card, previous)
        if refusals:
            console.print("[yellow]not publishable as the baseline:[/]")
            for r in refusals:
                console.print(f"  ⚠ {escape(str(r))}")
        else:
            console.print(f"[dim]publish it with:  nh bench publish {escape(out.name)}[/]")
        if halt.stopped:
            cmd = resume_command(full=full, quick=quick, limit=limit,
                                 specs_dir=specs_dir, label=label,
                                 parallel=parallel, trials=trials)
            console.print(f"[bold red]quota saturation — halted after "
                          f"{int(QUOTA_HALT_CONSECUTIVE_DEAD)} consecutive zero-token "
                          f"specs; {len(halt.dropped)} unscored spec(s) left to "
                          f"re-run[/]")
            console.print(f"[dim]resume with:  {escape(cmd)}[/]")
            sys.exit(1)
        if not result.passed:
            console.print("[bold red]north-star gate FAILED:[/]")
            for r in result.reasons:
                console.print(f"  ⛔ {escape(str(r))}")
            if gate:
                sys.exit(1)
        else:
            console.print(
                f"[bold green]north-star gate: {escape(str(result.reasons[0]))}[/]")

    asyncio.run(_go())


def _render_report_or_refuse(card) -> str:
    """Render the report, or REFUSE (exit 1) if it carries a banned term.

    Returns the markdown rather than writing it, so the CALLER decides the write
    order — a guard that runs after the baseline was already saved is not a
    guard, it is a partial publish.

    docs/NORTH_STAR_BENCH.md is TRACKED and its per-task `notes` column is
    judge-authored free text quoting real repo contents, so a publish can drop
    an internal codename into a clean file. That is not hypothetical: a v13
    publish put three of them there (an internal system name glued inside
    `<name>-pipeline`, another codename, and a competitor product) while the
    author's own manual scan of a shorter list reported clean.

    The test-suite guard cannot catch this — it is xfail(strict=False) while a
    vendor-neutral sweep is in progress, so it passes either way. A write path
    into a tracked file has to enforce its own precondition.
    """
    from ..eval.northstar_card import REPORT_MD, render_northstar_md
    from ..eval.vendor_terms import find_banned_terms

    md = render_northstar_md(card)
    found = find_banned_terms(md)
    # The report states "labels and repo paths are pseudonymised". The guard
    # checked vendor terms only, so a home path published happily underneath a
    # sentence asserting it had been removed — a self-certifying honesty claim
    # in the one artifact the project cites as proof.
    if str(Path.home()) in md or "/Users/" in md:
        found = found + ["<a home path>"]
    if found:
        # Redacted locators: enough to find the offending note, without
        # printing the term into a console log that may itself be pasted.
        # Redacted term shapes (first letter + length) so a console log that
        # gets pasted does not carry the term, PLUS the task_ids of the rows
        # that trip it — the shape alone cannot be grepped for.
        where = ", ".join(f"{t[0]}*({len(t)})" for t in found)
        # Look in every field that reaches the render, not just notes: a hit in
        # a project label or the card label refused with a count and no locator.
        def _dirty(text: str) -> bool:
            return bool(find_banned_terms(text)) or "/Users/" in text
        rows = [s.task_id for s in card.scores
                if _dirty(s.notes or "") or _dirty(s.project or "")]
        if _dirty(card.label or ""):
            rows.append(f"(run label {card.label!r})")
        # NO SQUARE BRACKETS around `where`. They were literals in the format
        # string, but rich reads `[f*(13)]` as a markup tag and DELETES it —
        # and `where` is always lowercase-initial (every banned term is), so
        # the locator was eaten on every real refusal without exception. The
        # locator is the whole reason `where` exists: without it the operator
        # is told a term was found and never which one. Escaping the brackets
        # would also work; dropping them removes the class instead of patching
        # this instance, and `where` itself is a redacted shape with no
        # brackets of its own.
        console.print(
            f"[bold red]refusing to publish:[/] the rendered report would "
            f"contain {len(found)} disallowed string(s): {where}")
        if rows:
            # escape LAST, over the joined string. Two branches arrived at
            # this same line independently, for two real failure modes of the
            # same class: `rows` carries `(run label {card.label!r})`, and a
            # label is free operator text. A label holding a `/Users/` path is
            # hostile rich markup — unescaped, a guaranteed crash on the exact
            # input this guard exists to report, swallowing the locator and
            # the "edit the results JSON" guidance underneath. A lowercase
            # bracketed span like `probe [rerun]` is read by rich as a markup
            # tag and DELETED, so the row names a label the operator never
            # wrote and cannot search for. escape() the interpolated content
            # only — `[dim]...[/]` stays outside it and still styles.
            console.print(
                f"[dim]offending row(s): {escape(', '.join(rows[:8]))}[/]")
        console.print(
            "[dim]the per-task notes are judge-authored free text quoting real "
            "repo contents. Edit the results JSON's `notes` for those rows and "
            "re-publish; --force does NOT override this guard.[/]")
        sys.exit(1)
    return md


@bench.command("startup")
@click.option("--scenario", "scenario_path", default=None,
              type=click.Path(path_type=Path),
              help="Scenario file (default: eval/startup_scenario/parcelo.yaml).")
@click.option("--out", "out_dir", default=None, type=click.Path(path_type=Path),
              help="Where to build the sprint (default: a fresh temp dir). The "
                   "codebase is fictional and disposable — rebuild it per run.")
@click.option("--verdict", "verdict_file", default=None, type=click.Path(),
              help="Grade a finished run instead of building one: pass the "
                   "results JSON `nh bench run` wrote.")
def bench_startup(scenario_path, out_dir, verdict_file):
    """Build the startup-company sprint, or grade a finished run of it.

    The scenario is one fictional company's codebase and an ORDERED sprint of
    related tickets. Building it writes the git history and one ordinary bench
    spec per ticket, each PINNED to the commit where the preceding tickets are
    merged — so `nh bench run --specs-dir` replays a sprint with no second
    runner and no special-casing.

    Ticket order is load-bearing, so run it with `--parallel 1`.
    """
    import tempfile

    from ..eval.northstar_card import NorthStarCard
    from ..eval.startup import (
        DEFAULT_SCENARIO,
        load_scenario,
        materialise,
        render_sprint_verdict,
        sprint_verdict,
        validate_scenario,
    )

    scenario = load_scenario(Path(scenario_path) if scenario_path
                             else DEFAULT_SCENARIO)
    problems = validate_scenario(scenario)
    if problems:
        console.print(f"[red]{escape(str(scenario.path))}: scenario is malformed[/]")
        for line in problems:
            console.print(f"  ⛔ {escape(line)}")
        sys.exit(1)

    if verdict_file:
        card = NorthStarCard.load(Path(verdict_file))
        if card is None:
            console.print(f"[red]no results at {escape(str(verdict_file))}[/]")
            sys.exit(1)
        verdict = sprint_verdict(card.scores, scenario)
        console.print(escape(render_sprint_verdict(verdict, scenario)))
        sys.exit(0 if verdict.passed else 1)

    dest = Path(out_dir) if out_dir else Path(
        tempfile.mkdtemp(prefix="nh-startup-"))
    if dest.exists() and any(dest.iterdir()):
        console.print(f"[red]{escape(str(dest))} is not empty — the sprint "
                      "history must be built from scratch, or the pins would "
                      "describe a tree that is not there[/]")
        sys.exit(1)
    sprint = materialise(scenario, dest)
    console.print(f"[green]{escape(scenario.name)}[/] — {len(sprint.specs)} ticket(s)")
    for position, spec in enumerate(sprint.specs, start=1):
        tag = " [yellow](must escalate)[/]" if spec.expect_escalation else ""
        console.print(f"  {position}. {escape(spec.id)} @ "
                      f"{escape(sprint.pins[spec.id][:8])}{tag}")
    console.print(f"[dim]repo  → {escape(str(sprint.repo))}[/]")
    console.print(f"[dim]specs → {escape(str(sprint.specs_dir))}[/]")
    console.print("\nrun the sprint:")
    console.print(f"  nh bench run --specs-dir {escape(str(sprint.specs_dir))} "
                  f"--parallel 1 --label startup-{escape(scenario.id)}")
    console.print("then grade it:")
    console.print("  nh bench startup --verdict "
                  "eval/results/northstar/<the-results-file>.json")
    console.print("[dim]--gate is not meaningful here: it grades coverage "
                  "against the curated north-star corpus, which a scenario run "
                  "is deliberately not part of.[/]")


@bench.command("publish")
@click.argument("results_file")
@click.option("--force", is_flag=True,
              help="Publish despite the refusals, recording them in the report.")
def bench_publish(results_file: str, force: bool):
    """Promote a results file to the baseline + docs/NORTH_STAR_BENCH.md.

    Publishing is an ACT, not a side effect of finishing a run. A saturated run
    and a one-spec probe have each overwritten the committed report; both were
    "clean completions" as far as the runner could tell.
    """
    from ..eval.northstar_card import (
        REPORT_MD, RESULTS_DIR, NorthStarCard, publish_refusals,
        published_file, render_northstar_md, success_headline,
    )
    path = Path(results_file)
    if not path.exists():
        path = RESULTS_DIR / results_file
    card = NorthStarCard.load(path)
    if card is None:
        console.print(f"[red]not a readable results file: {escape(str(path))}[/]")
        sys.exit(1)

    previous = NorthStarCard.load(RESULTS_DIR / "latest.json")
    refusals = publish_refusals(card, previous)
    if refusals and not force:
        console.print(f"[bold red]refusing to publish {escape(path.name)}:[/]")
        for r in refusals:
            # The narrowing refusal embeds `previous.label`, which is card-
            # authored — so this line was a live MarkupError, not a theoretical
            # one. Proven by seeding a baseline labelled `@[/Users/dev/base]`.
            console.print(f"  ⛔ {escape(str(r))}")
        console.print(
            "\n[dim]If you have judged this run publishable anyway, re-run with "
            "--force; the reasons are recorded in the report.[/]")
        sys.exit(1)

    card.override_reasons = refusals if force else []
    # Render and CHECK before either write. The guard used to run after
    # latest.json was already saved, so a refusal left a partial publish: the
    # gate would then compare every later run against a baseline whose report
    # was rejected, and `nh bench report` — which renders from latest.json —
    # would hard-fail forever, leaving no command able to regenerate the
    # tracked file. The sibling refusal path asserts exactly this invariant.
    md = _render_report_or_refuse(card)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    card.save(RESULTS_DIR / "latest.json")
    # SCRUM-25: only a publish that needed no override is a clean baseline —
    # keep it distinct from `latest.json` so a later `--force`d publish (a
    # probe, a saturated run) cannot erase the last trustworthy measurement.
    if not refusals:
        card.save(published_file())
    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(md)
    agg = card.as_dict()["aggregate"]
    if refusals:
        console.print("[bold yellow]published WITH --force over:[/]")
        for r in refusals:
            console.print(f"  ⚠ {escape(str(r))}")
    console.print(
        # This line runs AFTER latest.json, published_baseline and the report are
        # all written. Unescaped, a hostile label turned a completed publish into
        # a traceback with no "published" line — the operator sees exit 1 and a
        # crash while the tracked report has already been replaced. A post-write
        # crash is strictly worse than the pre-write one this branch set out to fix.
        f"[green]published[/] {escape(card.label or path.name)} — "
        f"success {escape(success_headline(card))} · "
        f"median cost ratio {agg['median_cost_ratio']} (basis: {agg['median_cost_ratio_basis']}) · "
        f"{agg['total_nh_tokens']:,} tokens → docs/NORTH_STAR_BENCH.md")


def _load_results_json(name: str) -> tuple[dict, Path]:
    """Load and SHAPE-CHECK a results file by path or bare filename.

    Same resolution `bench publish` uses, so `nh bench compare v13.json
    v14.json` works from anywhere in the repo. Exits 1 on an unreadable OR
    schema-drifted file: both are USAGE failures, not verdicts — the
    comparison itself never exits non-zero (see the command docstring).

    The shape check is not fussiness. A row missing `outcome_status` counts as
    RAN and a row missing `goal_satisfied` counts as FAILED, so a drifted file
    renders a confident wall of regressions indistinguishable from a real
    catastrophe; two files with no `scores` key at all render "0.0% of 0
    measured spec(s)" and exit 0. `validate_results` states the whole argument.
    """
    from ..eval.bench_compare import ResultsSchemaError, validate_results
    from ..eval.northstar_card import RESULTS_DIR
    path = Path(name)
    if not path.exists():
        path = RESULTS_DIR / name
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]not a readable results file:[/] "
                      f"{escape(str(path))} — {escape(str(exc))}")
        sys.exit(1)
    try:
        validate_results(data, source=path.name)
    except ResultsSchemaError as exc:
        console.print(f"[bold red]refusing to compare:[/] {escape(str(exc))}")
        sys.exit(1)
    return data, path


def _compare_side(label: str, created: str, rate: float, specs: int,
                  rederived: int = 0, recorded: bool = True) -> str:
    """One run's headline line, built as plain text so the caller can escape it
    whole — the label comes out of a results FILE and can hold `[/x]`.

    ``rederived`` defaults to 0 rather than being required: a results file
    predating `pin_rederived_spec_count` still has to render a line. But a
    default-0 count is ambiguous with a genuine zero, so ``recorded``
    (``Comparison.rederived_recorded_a/b``) disambiguates: when the field
    was never in that results file at all, this prints `unrecorded` instead
    of claiming `0 re-derived pin(s)` — a count this function was never
    actually handed."""
    if not recorded:
        rederived_text = "pin re-derivation: unrecorded (results file predates the field)"
    else:
        rederived_text = f"{rederived} re-derived pin(s)"
    return (f"{label or '(unlabelled)'} · {created or 'undated'} · "
            f"{rate:.1%} of {specs} measured spec(s) · "
            f"{rederived_text}")


@bench.command("compare")
@click.argument("run_a")
@click.argument("run_b")
@click.option("--canary", "canary_files", multiple=True, type=click.Path(),
              help="Extra results file(s) to fold into the flaky-canary scan. "
                   "The canary runs over RUN_A, RUN_B and these, sorted by "
                   "created_at, and lists specs whose verdict flipped across "
                   "2+ consecutive run-pairs. Repeatable; needs 3+ files total "
                   "to say anything the flip list does not.")
@click.option("--cost-top", "cost_top", type=int, default=10, show_default=True,
              # Literal, not `bench_compare.DEFAULT_COST_TOP`: every `..eval`
              # import in this command is LAZY (inside the function body), to
              # keep `nh` startup off the eval package's import graph, and a
              # click option default is evaluated at module load. Kept equal
              # to `DEFAULT_COST_TOP` by `tests/test_bench_compare.py`.
              help="How many specs to show in the cost-delta table, ranked by "
                   "absolute token delta. 0 shows every paired spec — this "
                   "never silently hides data, it only truncates the table.")
@click.option("--cost-threshold", "cost_threshold", type=float, default=1.1,
              show_default=True,
              # Literal for the same reason; kept equal to
              # `DEFAULT_COST_FLIP_RATIO` by the same test.
              help="Flag a flipped spec's cost movement when its "
                   "new/old token ratio is at or above this, or at or below "
                   "1/this.")
def bench_compare(run_a: str, run_b: str, canary_files, cost_top: int,
                  cost_threshold: float):
    """Compare two runs PAIRED per spec — RUN_A is the baseline, RUN_B the change.

    Two headline numbers cannot tell you whether a change helped: "90.7% →
    90.7%" is the same string whether nothing moved or five specs broke while
    five others got fixed. This pairs on task_id, prints the flips in both
    directions with their notes, names every spec that could NOT be paired,
    runs McNemar's exact test on the discordant pairs — beside the counts,
    because below 6 discordant pairs that test cannot reach p<0.05 at all —
    and shows each paired spec's token/cost delta, so a cost regression is
    attributable to a spec in minutes, not thrown away as a success bit.

    A REPORT, NOT A GATE: it writes nothing and always exits 0. The regression
    gate is `nh bench run --gate`; the publish refusals are in `bench publish`.
    """
    from ..eval.bench_compare import (
        MIN_DISCORDANT_FOR_POWER, compare_runs, cost_caveat, flaky_canary,
        headline_caveat, interpretation, undated_run_indices)

    card_a, path_a = _load_results_json(run_a)
    card_b, path_b = _load_results_json(run_b)
    cmp = compare_runs(card_a, card_b)

    console.print("[bold]paired per-spec comparison[/] "
                  f"({escape(path_a.name)} → {escape(path_b.name)})")
    console.print(f"  A (baseline) {escape(_compare_side(cmp.label_a, cmp.created_a, cmp.rate_a, cmp.specs_a, cmp.rederived_a, cmp.rederived_recorded_a))}")
    console.print(f"  B (change)   {escape(_compare_side(cmp.label_b, cmp.created_b, cmp.rate_b, cmp.specs_b, cmp.rederived_b, cmp.rederived_recorded_b))}")
    console.print(f"  [dim]{escape(headline_caveat())}[/]")
    console.print("")

    console.print(f"  paired specs: {int(cmp.paired)}  ·  "
                  f"both pass {int(cmp.both_pass)}  ·  "
                  f"both fail {int(cmp.both_fail)}  ·  "
                  # DISTINCT specs the pairing could not reach. A run compared
                  # against a baseline it shares half a corpus with is not a
                  # paired comparison, and this is the number that says so —
                  # the four lists below say which specs and why.
                  f"unpaired {int(cmp.unpaired)}")
    console.print(f"  [red]regressed (A✓→B✗) b={int(cmp.b_regressed)}[/]  ·  "
                  f"[green]fixed (A✗→B✓) c={int(cmp.c_fixed)}[/]")
    # Formatted OUTSIDE the print: an `escape(f"{x:.4f}")` nested inside the
    # print's own f-string still contains an un-escaped interpolation node, and
    # the AST guard in tests/_bench_ast_guard.py reads nodes, not intent.
    p_str = f"{cmp.p_value:.4f}"
    console.print(f"  McNemar exact, two-sided: p = {escape(p_str)} on "
                  f"{int(cmp.discordant)} discordant pair(s) "
                  f"(power floor: {int(MIN_DISCORDANT_FOR_POWER)})")
    console.print(f"  [yellow]{escape(interpretation(cmp))}[/]")

    # UNPAIRED SPECS ARE NAMED, not just counted. A spec present in one run and
    # not the other contributes to neither cell of the 2x2 and would otherwise
    # vanish into "no change" — which is exactly how a corpus that half stopped
    # resolving reads as a clean comparison.
    for title, ids in (("only in A", cmp.only_in_a), ("only in B", cmp.only_in_b),
                       ("unmeasured in A (all trials skipped)", cmp.unmeasured_a),
                       ("unmeasured in B (all trials skipped)", cmp.unmeasured_b)):
        if ids:
            console.print(f"  [dim]{escape(title)}: {int(len(ids))} — "
                          f"{escape(', '.join(ids))}[/]")

    flips = cmp.regressions + cmp.fixes
    if flips:
        console.print("")
        console.print("  | task | title | direction | A | B | trials flipped "
                      "| notes |")
        console.print("  |---|---|---|---|---|---|---|")
        for f in flips:
            # A spec's TITLE is the only cell a reader recognises a task by —
            # `ns-cbb81747` names nothing on its own. It is also the field that
            # produced the v11 MarkupError (one real title begins `@[/Users/…]`),
            # so it is truncated and escaped like every other file-authored cell.
            title = (f.title or "")[:40].replace("|", "/")
            cell_a = f"{f.a.outcome_status} {f.a.passes}/{f.a.trials}"
            cell_b = f"{f.b.outcome_status} {f.b.passes}/{f.b.trials}"
            note = (f.b.notes or f.a.notes or "")[:70].replace("|", "/")
            console.print(
                f"  | {escape(f.task_id)} | {escape(title)} | "
                f"{escape(f.direction)} | "
                f"{escape(cell_a)} | {escape(cell_b)} | "
                f"{int(f.trial_flips)}/{int(f.trials_paired)} | "
                f"{escape(note)} |")
    else:
        console.print("  [dim]no spec changed verdict between these two runs[/]")

    # COST, thrown away by every summary above this line: a spec that flipped
    # for free and a spec that flipped and got 3x more expensive both read as
    # "1 regression" in the counts and the table. This section is the reason
    # a real cost regression (per-spec ratio 0.107 -> 0.336) went unattributed
    # for a full release cycle.
    console.print("")
    if cmp.specs_costed == 0:
        console.print(f"  [dim]{escape(cost_caveat())}[/]")
        console.print("  [dim]no spec on either side of this comparison "
                      "carries cost data[/]")
    else:
        agg = cmp.aggregate_token_delta
        # Formatted OUTSIDE the print, same reason as `p_str` above: an
        # `escape(f"...")` nested inside the print's own f-string still
        # contains an un-escaped interpolation node to the AST guard.
        agg_str = "n/a" if agg is None else f"{agg:+.0f}"
        console.print(
            f"  [bold]cost[/] aggregate priced-token delta (sum, B-A): "
            f"{escape(agg_str)}  ·  {int(cmp.specs_costed)} spec(s) costed  "
            f"·  {int(cmp.specs_missing_cost)} spec(s) missing cost")
        console.print(f"  [dim]{escape(cost_caveat())}[/]")

        top = cmp.top_cost_deltas(cost_top)
        shown = [d for d in top if d.token_delta is not None]
        if shown:
            console.print("")
            console.print("  | task | title | A tokens | B tokens | delta "
                          "| ratio (B/A) |")
            console.print("  |---|---|---|---|---|---|")
            for d in shown:
                title = (d.title or "")[:40].replace("|", "/")
                a_tok = "n/a" if d.a is None or d.a.priced_tokens is None \
                    else f"{d.a.priced_tokens:.0f}"
                b_tok = "n/a" if d.b is None or d.b.priced_tokens is None \
                    else f"{d.b.priced_tokens:.0f}"
                delta_str = f"{d.token_delta:+.0f}"
                ratio = d.movement_ratio
                ratio_str = "n/a" if ratio is None else f"{ratio:.2f}x"
                console.print(
                    f"  | {escape(d.task_id)} | {escape(title)} | "
                    f"{escape(a_tok)} | {escape(b_tok)} | "
                    f"{escape(delta_str)} | {escape(ratio_str)} |")
            if cost_top > 0 and len(cmp.cost_deltas) > cost_top:
                remaining = len(cmp.cost_deltas) - cost_top
                console.print(
                    f"  [dim]… {int(remaining)} more paired spec(s) not "
                    f"shown — raise --cost-top to see them[/]")

        threshold_str = f"{cost_threshold:.2f}"
        flagged = cmp.cost_flagged(cost_threshold)
        if flagged:
            console.print("")
            for d in flagged:
                f_title = (d.title or "")[:40]
                ratio = d.movement_ratio
                ratio_str = "n/a" if ratio is None else f"{ratio:.2f}x"
                console.print(
                    f"  [yellow]⚠ {escape(d.task_id)} {escape(f_title)} "
                    f"flipped AND cost moved {escape(ratio_str)} "
                    f"(threshold {escape(threshold_str)}x)[/]")

    if canary_files:
        history = [card_a, card_b]
        names = [path_a.name, path_b.name]
        for extra in canary_files:
            data, extra_path = _load_results_json(str(extra))
            history.append(data)
            names.append(extra_path.name)
        canaries = flaky_canary(history)
        console.print("")
        console.print(f"  [bold]flaky canary[/] over {int(len(history))} run(s), "
                      "ordered by created_at — a spec that flips across 2+ "
                      "consecutive run-pairs is noise, not a result "
                      "(repetition, not isolation, decides)")
        # "ordered by created_at" must not be asserted over runs that have no
        # date. They are placed LAST rather than dropped, and named here, so a
        # reader can see that the chain's tail is an assumption and not a
        # measurement — an undated run in a different position changes which
        # runs are adjacent, and therefore which specs flip.
        undated = [names[i] for i in undated_run_indices(history)]
        if undated:
            console.print(
                f"  [yellow]⚠ {int(len(undated))} run(s) carry no created_at "
                f"and were placed LAST, in the order given: "
                f"{escape(', '.join(undated))} — the chain's order is only as "
                f"good as the dates on it[/]")
        if not canaries:
            console.print("  [dim]no spec flipped in 2+ consecutive run-pairs[/]")
        for c in canaries:
            c_title = (c.title or "")[:40]
            console.print(f"  ⚠ {escape(c.task_id)} {escape(c_title)} — "
                          f"{int(c.flips)} flip(s) over {int(c.pairs)} "
                          f"pair(s): {escape(' '.join(c.history))}")
    # No sys.exit: a report never decides anything. The gate is `run --gate`.


def _load_reviewer_recall_runner():
    """Load eval/reviewer_recall/runner.py by file path.

    That tree sits outside ``src/no_human`` on purpose (SCRUM-29 — "single
    surface: eval/ CLI python only"), so it is loaded dynamically rather than
    imported as a package. This function and the ``--reviewer-recall`` flag
    below are the ONLY things in this file allowed to reference it —
    tests/test_reviewer_recall_guard.py pins that nothing else does.
    """
    import importlib.util

    repo_root = Path(__file__).resolve().parents[3]
    runner_path = repo_root / "eval" / "reviewer_recall" / "runner.py"
    if not runner_path.exists():
        console.print(f"[red]reviewer-recall runner not found at {escape(str(runner_path))}[/]")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location(
        "nh_eval_reviewer_recall_runner", runner_path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: the runner's dataclasses resolve their (string,
    # via __future__ annotations) field types through
    # sys.modules[cls.__module__] — unregistered, py3.12 dies with
    # "'NoneType' object has no attribute '__dict__'".
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, repo_root


@bench.command("report")
@click.option("--reviewer-recall", is_flag=True,
             help="Score the fresh-context reviewer against the seeded-defect "
                  "corpus instead (SCRUM-29, docs/REVIEWER_RECALL_METHOD.md).")
def bench_report(reviewer_recall: bool):
    """Re-render docs/NORTH_STAR_BENCH.md from the latest saved results."""
    if reviewer_recall:
        config, _ = _bootstrap()
        module, repo_root = _load_reviewer_recall_runner()
        # markup=False: the per-class breakdown is bracketed ("[logic 2/2, …]")
        # and rich would otherwise swallow it as a style tag.
        try:
            text = module.run_and_report(repo_root, model=config.review_model)
        except module.HeadlineRefusedError as exc:
            # SCRUM-47's refusal is the correct outcome for a broken checkout —
            # surface it as a clean refusal, not a traceback.
            console.print(f"[red]recall headline refused:[/] {escape(str(exc))}")
            sys.exit(1)
        except module.TranscriptOverwriteRefused as exc:
            console.print(f"[red]recall run refused:[/] {escape(str(exc))}")
            sys.exit(1)
        console.print(text, markup=False)
        return

    from ..eval.northstar_card import (
        REPORT_MD, RESULTS_DIR, NorthStarCard, pin_rederivation_note,
        publish_refusals, render_northstar_md)

    card = NorthStarCard.load(RESULTS_DIR / "latest.json")
    if card is None:
        console.print("[yellow]no results yet — run `nh bench run` first[/]")
        _print_pr_outcome_block()
        sys.exit(1)

    # The SECOND of the two write paths into the tracked report (the other is
    # bench_publish), and it used to be the one that never asked whether the
    # card deserved to be there. Two paths, two commands — `--force` is a flag
    # on the first, not a third writer.
    # `bench publish` refuses a probe; `bench report` rendered the same card and
    # wrote it, so the refusal was one command away from irrelevant.
    #
    # The test is `override_reasons`, NOT the refusals alone. A force-published
    # card carries them and must keep re-rendering — otherwise a forced baseline
    # can never be regenerated, which is the failure the publish path's own
    # write-ordering comment warns about. A card carrying refusals and NO
    # override record never passed a human, so it is the one to refuse.
    refusals = publish_refusals(card)
    if refusals and not card.override_reasons:
        # escape(): the label is model- and file-authored, and a refusal that
        # crashes is not a refusal. `@[/Users/dev/probe]` reads to rich as a
        # closing tag and raises MarkupError — the v11 crash class
        # tests/test_bench_print_escape.py exists for. The write is guarded
        # either way (the print precedes it), but a traceback in place of a
        # clean "here is why I refused" is how a guard stops being read.
        console.print(
            f"[bold red]refusing to re-render from "
            f"{escape(card.label or 'latest.json')}:[/]")
        for r in refusals:
            console.print(f"  ⛔ {escape(str(r))}")
        console.print(
            "\n[dim]latest.json holds a run that was never published — nothing "
            "here was overwritten. To publish it anyway, and record why in the "
            "report itself, use `nh bench publish <results-file> --force`.[/]")
        # The PR-outcome block is printed on the REFUSAL path too, and that is
        # deliberate. It reads a different population (real tasks) from a
        # different source (the `pr_outcomes` table) than the card being
        # refused, so the card's verdict says nothing about whether these
        # figures are sound. Gating it behind a successful render would hide
        # the honest number behind an unrelated guard — and in practice every
        # results file currently on disk is refused by the checks above, so it
        # would almost never print at all. The refusal text stays first and
        # intact, and the exit code is unchanged.
        _print_pr_outcome_block()
        sys.exit(1)

    REPORT_MD.write_text(_render_report_or_refuse(card))
    console.print(f"[green]report rendered[/] → {REPORT_MD}")
    console.print(escape(pin_rederivation_note(card)))
    _print_pr_outcome_block()


def _print_pr_outcome_block() -> None:
    """Print delivered/merged/unknown beside the bench report — never inside it.

    TWO POPULATIONS, AND THEY MUST NOT BE ADDED UP. The card above scores the
    bench corpus, whose specs run in a sandbox that pushes to a LOCAL BARE REPO
    (`vcs/__init__.py` hands those a `local-pr://` marker). There is no forge in
    that path, so no bench spec can ever merge, and a "merge rate" computed over
    the corpus would be a structural zero dressed as a finding. `delivered` is
    the most the corpus can show.

    The block below is therefore drawn from the REAL task database instead, and
    it is printed to the console rather than written into
    `docs/NORTH_STAR_BENCH.md`: that file is tracked and must stay reproducible
    from `latest.json`, and these numbers are machine-local. Writing them in
    would make a committed artefact depend on whose laptop rendered it.
    """
    from ..core.autonomy import compute_pr_outcome_metrics, render_pr_outcome_lines

    try:
        config, _ = _bootstrap(require_auth=False)

        async def _go():
            async with Store(config.db_path) as store:
                return await compute_pr_outcome_metrics(store)

        rep = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001 — a telemetry block must not fail `bench report`
        console.print(f"[dim]pr-outcome block unavailable: {escape(str(exc))}[/]")
        return

    console.rule("[bold]PR outcome — the REAL task database, not the bench corpus")
    console.print(
        "[dim]Separate population from the card above. Bench specs push to a "
        "local bare repo and can never merge, so 'delivered' is the ceiling "
        "there; these figures come from real runs.[/]")
    for line in render_pr_outcome_lines(rep):
        console.print(escape(line), markup=False)


@cli.command("shadow")
@click.argument("title")
@click.option("--repo", required=True, type=click.Path(exists=True), help="Target repo.")
@click.option("--criteria", multiple=True, help="Acceptance criterion (repeatable).")
def shadow_cmd(title, repo, criteria):
    """Shadow-run a task end-to-end in a sandbox clone WITHOUT pushing (21.3)."""
    config, _ = _bootstrap()
    from ..agent.claude_backend import ClaudeBackend
    from ..eval import run_shadow
    from ..review.reviewer import AdversarialReviewer

    backend = ClaudeBackend(
        model=config.primary_model,
        forbidden_paths=config["safety"]["forbidden_paths"],
        never_push_to=config["git"]["never_push_to"],
    )

    async def _go():
        result = await run_shadow(
            config.data, repo_path=str(Path(repo).resolve()), task_title=title,
            backend=backend, acceptance_criteria=list(criteria),
            reviewer=AdversarialReviewer.from_config(config.data),
            on_event=render_event,
        )
        console.rule(f"[bold]shadow: {result.outcome_status}")
        console.print(f"[dim]{result.notes}[/]")
        console.print(result.draft_diff[:8000] or "(no diff produced)")

    asyncio.run(_go())


@cli.command("test")
@click.argument("mode", default="fast", type=click.Choice(["fast", "full", "slow"]))
@click.option("-v", "--verbose", is_flag=True, help="Show full pytest output.")
def test_cmd(mode, verbose):
    """Run the test suite locally (zero LLM tokens).

    Modes:

      fast  — 711 tests, ~28s (skip slow integration tests)
      full  — 721 tests, ~3min (all tests, parallel)
      slow  — 10 tests, ~3min (eval replay + integration only)

    This runs pytest directly as a subprocess — no agent turns, no token cost.
    Use this instead of running 'uv run pytest' inside an AI session.
    """
    import subprocess as _sp

    project_root = Path(__file__).resolve().parents[3]
    script = project_root / "scripts" / "run_tests.sh"

    if script.exists():
        cmd = [str(script), mode]
    else:
        # Fallback if script missing.
        marker_args = {
            "fast": ["-m", "not slow"],
            "slow": ["-m", "slow"],
            "full": [],
        }[mode]
        cmd = ["uv", "run", "pytest", "-q", "--tb=short", "-n", "auto"] + marker_args

    console.print(f"[bold blue]nh test {mode}[/] — running (no LLM tokens spent)")
    if not verbose:
        cmd_str = " ".join(cmd)
        console.print(f"[dim]  {cmd_str}[/]")

    result = _sp.run(cmd, cwd=project_root)

    if result.returncode == 0:
        console.print(f"\n[bold green]✓ All tests passed[/] (mode={mode})")
    else:
        console.print(f"\n[bold red]✗ Tests failed[/] (exit {result.returncode})")
        sys.exit(result.returncode)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
