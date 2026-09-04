"""FastAPI board + approval API for no_human.

Exposes:
  GET  /api/tasks            — board overview (all tasks, summarised)
  GET  /api/tasks/{id}       — full task detail + attempts + review checklist
  GET  /api/tasks/{id}/diff  — git diff for the latest attempt's commit
  POST /api/tasks/{id}/approve   — record human approval (agent never merges)
  POST /api/tasks/{id}/send-back — store feedback, reset task for retry
  WS   /ws                   — live board updates (sync every 2 s)

Static files (the React board) are served from ../../../web/dist when present.
"""

from __future__ import annotations

import asyncio
import copy
import json
import contextlib
import os
import posixpath
import re
import subprocess
import threading
import time
from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # import-cycle-free: the eval package is loaded lazily below
    from ..eval.northstar_card import NorthStarCard

import httpx
from fastapi import (
    FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..agent.session_mark import AGENT_SESSION_HEADER, request_is_marked
from .local_boundary import (
    LOOPBACK_ORIGIN_REGEX, allowed_hosts, install_local_boundary,
    require_local_origin, ws_handshake_is_local,
)
from ..blockers import process_actor
from ..config import _atomic_write_text, load_config
from ..core.db import Store
from ..core.lanes import lane_for
from ..core.bounds import Bounds
from ..core.orchestrator import Orchestrator, is_agent_session, is_narration
from ..core.pricing import weighted_tokens
from ..core.task import Task, TaskStatus, normalise_priority
from ..vcs.task_pr import task_has_pr_evidence
from .models import (
    AttemptDetailsOut, AttemptOut, BoardPayload, BudgetOut, CancelRequest, CreateProjectRequest,
    CreateTaskRequest, GrillQuestionOut, GrillResultOut, GrillStepRequest, IntegrationSetupRequest,
    ImportedInfo, LandedOverrideRequest, PhaseOut, ProjectOut, ReplyRequest,
    SaveIntegrationConfigRequest, SendBackRequest, ShippedRequest, SplitRequest, TaskOut,
    TaskSummaryOut, TelemetryConsentRequest, TrackerIssueOut, UpdateProjectRequest,
)

import logging

log = logging.getLogger("no_human.api")

# Read the platform through a constant, never an inline `os.name` test, so the
# Windows branches below are reachable from a test on any host. No Windows
# machine or runner is available to this project.
_IS_WINDOWS = os.name == "nt"

def _resolve_web_dist() -> Path:
    """Locate the built React board across the three ways this code ships.

    There is no single path that works for all three, so each is tried in turn:

    1. **Repo checkout / frozen desktop bundle** — ``parents[3]/web/dist``.
       In a checkout ``__file__`` is ``<repo>/src/no_human/api/app.py``, so
       parents[3] is the repo root. Under a PyInstaller onedir freeze it is
       ``<bundle>/_internal/no_human/api/app.py``, so parents[3] is the bundle
       root, which is where ``packaging/build-installer.sh`` copies the board.
       Both land on ``web/dist`` with no change to this line — that equivalence
       is deliberate and ``packaging/nh-server.spec`` depends on it.
    2. **Wheel install** — ``<site-packages>/no_human/web_dist``. parents[3] is
       meaningless there (it points at ``lib/python3.X``, outside the package),
       so the board is shipped INSIDE the package instead. ``pyproject.toml``
       force-includes ``web/dist`` to that name at build time.

    Returning the first candidate that exists means a repo checkout never sees
    a stale wheel-style copy and vice versa: at most one of these ever exists.
    The first candidate is returned as the fallback when neither is present, so
    the "board was never built" message names the path a developer expects.
    """
    candidates = (
        Path(__file__).resolve().parents[3] / "web" / "dist",  # checkout / frozen
        Path(__file__).resolve().parent.parent / "web_dist",   # installed wheel
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[0]


_WEB_DIST = _resolve_web_dist()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    # Pin the loaded-code snapshot HERE, before anything can run, so it is the
    # sha of what this process holds in memory rather than of whatever HEAD
    # happens to be the first time something asks. The server never reloads:
    # every attempt this process records will carry this value.
    # Off the event loop: this is four git subprocesses (~294ms measured, and
    # 40s in the worst case the timeouts allow). Startup is exactly when the
    # loop has other things to do.
    from ..core.build_info import loaded_code, staleness_note
    code = await asyncio.to_thread(loaded_code)
    app.state.loaded_code = code.descriptor
    log.info("loaded code: %s", code.descriptor)
    # WARNING level on purpose: uvicorn runs at log_level="warning", so INFO is
    # dropped and only the line that has something to say survives. Advisory —
    # nothing below reads it, and no task is prevented from being claimed.
    # This line alone is NOT the surface: it scrolls past at boot, and the case
    # that matters is a server that has been up for hours. See the board banner
    # fed by /api/worker/status.
    _startup_stale = await asyncio.to_thread(staleness_note, code)
    if _startup_stale:
        log.warning("%s", _startup_stale)
    # Seed `_stale_cache` here too, off the loop, so the first `/api/worker/
    # status` poll is not silent (a cold `_stale_cache` reads as "current" —
    # see `_loaded_code_stale`'s docstring). This is the SAME git call
    # `staleness_note` above just made; `_loaded_code_stale` re-measures HEAD
    # rather than reusing `_startup_stale` because HEAD (and thus the cache
    # key) can already differ by the time this line runs.
    await asyncio.to_thread(_loaded_code_stale)
    # `nh start` may already have connected a shared Store to hand to its
    # Jira/Linear intake pollers (started before uvicorn's ASGI lifespan
    # fires) — reuse it instead of opening a SECOND aiosqlite connection to
    # the same file. Two connections racing this lifespan's own connect+
    # migrate, with no busy_timeout set, is what flooded a clean `nh start`
    # with `sqlite3.OperationalError: database is locked` (KI, 2026-08-01).
    # One-shot handoff (popped, not just read) so a later lifespan cycle in
    # the same process never reuses an already-closed store.
    external_store = getattr(app.state, "_external_store", None)
    if external_store is not None:
        del app.state._external_store
    store = external_store or await Store(config.db_path).connect()
    app.state.store = store
    app.state.config = config
    # Setup mode: no subscription credential on file at all. `nh start` may
    # already have computed this (setup_reason printed at boot, before this
    # lifespan even fires) — OR it together with what THIS process sees now,
    # so a credential added between CLI bootstrap and lifespan firing still
    # lifts it, and a bare `TestClient(app)` construction (no CLI involved)
    # gets its own correct answer.
    from ..config import subscription_credential_missing
    _reason = subscription_credential_missing(config.data)
    app.state.setup_mode = bool(_reason) or bool(getattr(app.state, "setup_mode", False))
    app.state.setup_reason = _reason or getattr(app.state, "setup_reason", None)
    # Wiki jobs a previous process left queued/running are orphans now — fail
    # them so the board shows the truth instead of a job stuck "running"
    # forever (mirrors the scheduler's orphan recovery). Advisory: a failure
    # here must not block startup.
    try:
        from ..wiki_jobs import resume_unfinished
        await resume_unfinished(store)
    except Exception as exc:  # noqa: BLE001
        log.warning("wiki-job orphan recovery skipped: %s", exc)
    # CSP is computed once per app start from the loaded config: strict by
    # default, widened by exactly the PostHog hosts when the operator opted in.
    app.state.csp = _build_csp(config.data)
    # Opt-in telemetry (default OFF — record() no-ops without consent).
    try:
        from .. import telemetry as _telemetry
        _telemetry.record("app_started", config=config.data)
    except Exception:
        pass

    # Always start the embedded worker — board up = worker up.
    # CLI may override max_workers/poll_interval via app.state._worker_opts.
    from ..core.runtime import build_orchestrator
    from ..core.scheduler import Scheduler, resolve_max_workers

    def _orch_factory(task=None):
        # ONE construction site for CLI and server alike (core/runtime).
        # The server used to hardcode ClaudeBackend here, so a task run
        # through the GUI ignored `worker.backend` while the same task via
        # `nh` honoured it (audit A8/X2, 2026-08-11).
        return build_orchestrator(config, store, task=task)

    overrides = getattr(app.state, "_worker_opts", None) or {}
    conc = config.data.get("concurrency", {})
    max_workers, worker_warning = resolve_max_workers(
        config.data, override=overrides.get("max_workers"))
    if worker_warning:
        log.warning("%s", worker_warning)
    # Bound pytest-xdist so N parallel tasks each running `pytest -n auto`
    # don't oversubscribe the CPU (child test subprocesses inherit this).
    from ..core.scheduler import bounded_xdist_workers
    _cap = bounded_xdist_workers(
        max_workers, os.cpu_count() or 2,
        os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"))
    if _cap is not None:
        os.environ["PYTEST_XDIST_AUTO_NUM_WORKERS"] = _cap
        log.info("bounded pytest-xdist auto workers to %s (%d task workers)",
                 _cap, max_workers)
    raw_poll = overrides.get("poll_interval") or conc.get("poll_interval", 10)
    try:
        poll_interval = float(raw_poll)
    except (ValueError, TypeError):
        # Handle "10s", "30s" style strings.
        import re as _re
        m = _re.match(r"(\d+)", str(raw_poll))
        poll_interval = float(m.group(1)) if m else 10.0

    # Optional wake watcher for auto-resuming blocked tasks.
    watcher = None
    try:
        from ..blockers import WakeWatcher
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
        )
    except Exception as exc:  # noqa: BLE001
        # B2 #13: this used to swallow silently — parked tasks are
        # notify-silent BY DESIGN and depend entirely on the watcher to wake,
        # so a dead watcher meant tasks BLOCKED forever with nobody told.
        # Loud log + a board-visible flag (surfaced via /api/worker).
        log.error("WakeWatcher failed to start — parked tasks will NOT wake "
                  "until the server restarts cleanly: %s", exc)
        app.state.watcher_error = str(exc)[:200]

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

    # Memory lifecycle C: the daily unconfirmed-proposal sweep (AC1).
    # `enabled: False` is honoured by passing None instead of constructing
    # the job — same shape as `reanalysis` above. Numeric coercions are
    # wrapped: a malformed config.yaml value here (`float("abc")`) must not
    # take the whole lifespan/board down with it.
    retirement_job = None
    learning_cfg = config.data.get("learning", {})
    if learning_cfg.get("sweep_enabled", True):
        try:
            from ..core.scheduler import RetirementSweepJob
            retirement_job = RetirementSweepJob(
                store,
                interval_seconds=float(
                    learning_cfg.get("sweep_interval_seconds", 86400)),
                archive_after_days=int(
                    learning_cfg.get("archive_unconfirmed_days", 45)),
                # D3 (2026-08-31 operator directive): the same kill switch
                # `learning.auto_manage` gates on the HarvestJob side —
                # threaded here too so `False` turns off BOTH the
                # auto-activation write path and its 90-day auto-retirement
                # read path in one config flip.
                auto_manage=bool(learning_cfg.get("auto_manage", True)),
                auto_retire_days=int(
                    learning_cfg.get("retire_suggest_days", 90)),
            )
        except (TypeError, ValueError) as exc:
            log.error("bad learning.* sweep config — retirement sweep "
                      "disabled this run: %s", exc)
            retirement_job = None

    # Dispatch-time gate: `assert_subscription_mode` (scrub + enforce) still
    # runs before any RUNNING task, just moved from "must pass before the
    # server boots" to "must pass before each dispatch" — so setup mode
    # never spends a token, and a credential added mid-run is picked up on
    # the very next tick with no restart.
    from ..config import assert_subscription_mode as _assert_sub_mode
    _llm_cfg = config.data.get("llm") or {}

    def _auth_check() -> None:
        _assert_sub_mode(
            profile=_llm_cfg.get("auth_profile"),
            auth_mode=_llm_cfg.get("auth_mode", "subscription"),
        )

    sched = Scheduler(
        store, _orch_factory,
        max_workers=max_workers,
        wake_watcher=watcher,
        on_event=lambda k, t: log.info("worker: %s — %s", k, t),
        reanalysis_job=reanalysis,
        retirement_job=retirement_job,
        config=config.data,
        auth_check=_auth_check,
    )
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(
        sched.run_forever(stop=stop_event, poll_interval=poll_interval)
    )

    def _worker_died(task: "asyncio.Task") -> None:
        """Record the worker loop's death where a human can see it.

        WITHOUT THIS THE SERVER LIES FOREVER. `create_task` returns a task
        nobody awaits until shutdown, so an exception inside `run_forever` is
        never retrieved: the coroutine stops, `app.state.scheduler` keeps
        answering, and `/api/worker/status` reports `running: true` for the rest
        of the process's life. asyncio's only complaint is a
        "Task exception was never retrieved" line at garbage-collection time.

        This is not hypothetical, and it is the incident's own mechanism one
        step earlier: `run_forever` awaits `_recover_orphans()` BEFORE its loop
        with no try/except, and that method issues unguarded writes. A
        connection wedged at startup — the inferred first cause of the very
        wedge this release detects — fails those writes with `database is
        locked` and kills the loop before its first tick.

        The endpoint reads `worker_error` and drops `healthy`, exactly as it
        already does for `watcher_error`.
        """
        if task.cancelled():
            return                       # ordinary shutdown, not a failure
        exc = task.exception()
        if exc is None:
            # Returned early without raising. Still fatal — nothing ticks again
            # — and still silent, so it is reported too.
            app.state.worker_error = (
                "the worker loop exited on its own without an error; no task "
                "will be dispatched until the server is restarted")
            log.error("%s", app.state.worker_error)
            return
        app.state.worker_error = f"{type(exc).__name__}: {exc}"
        log.error("THE WORKER LOOP DIED — no task will be dispatched until the "
                  "server is restarted: %s", exc, exc_info=exc)

    # Cleared BEFORE the callback is registered, not after. Safe either way
    # today only because no `await` separates the two lines, so the callback
    # cannot run in between; insert one and a death recorded by `_worker_died`
    # would be silently wiped back to None.
    app.state.worker_error = None
    worker_task.add_done_callback(_worker_died)
    app.state.scheduler = sched
    app.state.worker_stop = stop_event
    log.info("embedded worker started: %d worker(s), poll=%ds",
             max_workers, int(poll_interval))

    # Keeps `_stale_cache` warm without a request ever paying for the git
    # calls: `/api/worker/status` used to run `_loaded_code_stale` itself
    # (one `rev-parse`, and a `merge-base` too once behind) on every request
    # that won its single-flight lock, which is where the 5-14s stalls this
    # task fixes came from. This loop is the only remaining caller.
    stale_refresh_task = asyncio.create_task(_refresh_stale_note())
    app.state.stale_refresh_task = stale_refresh_task

    yield

    stale_refresh_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stale_refresh_task

    if worker_task and stop_event:
        stop_event.set()
        # `run_forever` asks every running attempt to checkpoint, then drains
        # for `concurrency.stop_grace_s`. Wait a margin LONGER than that, so
        # the scheduler's bounded drain (which logs what it abandoned) is what
        # ends this wait — not a second, shorter literal here.
        from ..core.scheduler import LIFESPAN_DRAIN_MARGIN_S, stop_grace_s
        budget = stop_grace_s(config.data) + LIFESPAN_DRAIN_MARGIN_S
        try:
            await asyncio.wait_for(worker_task, timeout=budget)
        except asyncio.TimeoutError:
            log.warning("worker drain timed out after %.0fs", budget)
    # An externally-supplied store is owned by whoever connected it (`nh
    # start`'s `_go()`) — it closes it, not us, or `start()`'s own use of the
    # connection after `server.serve()` returns would hit a closed store.
    if external_store is None:
        await store.close()


# The OpenAPI document's version is READ from the package rather than written
# here. It was a third hardcoded literal — `0.1.0` — and it stayed 0.1.0 through
# a release that moved `__version__` and `pyproject.toml`, so `/openapi.json`
# and `/docs` reported a version the build had left behind. A literal that only
# a generated document shows is exactly the kind nobody notices is stale.
from ..integrations.health import with_health_probes
app = FastAPI(title="no_human board", version=__version__, lifespan=with_health_probes(lifespan))
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=LOOPBACK_ORIGIN_REGEX,  # loopback only: api/local_boundary.py
    allow_methods=["*"],
    allow_headers=["*"],
)

# The board is fully self-contained (self-hosted fonts, data: favicon, ws
# socket) — say so on every response, so an injected external script/style/
# frame can never load (electron-pro checklist; fonts+CSP increment). React
# style attributes need 'unsafe-inline' in style-src; scripts stay strict
# (the built index.html has no inline script).
_CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self' ws: wss:; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)

# The two PostHog hosts the browser needs when (and ONLY when) the operator
# has opted in to Usage insights: the recorder/client bundle is fetched from
# us-assets, events + replay payloads POST to both. Exact hosts, nothing wider.
_POSTHOG_SCRIPT_HOST = "https://us-assets.i.posthog.com"
_POSTHOG_CONNECT_HOSTS = "https://us.i.posthog.com https://us-assets.i.posthog.com"


def _build_csp(config_data: dict) -> str:
    """The CSP header value for this app start. With telemetry off (the
    default) this returns `_CSP` UNCHANGED — byte-identical, a test pins it.
    With telemetry configured+enabled, script-src/connect-src gain exactly
    the PostHog hosts above."""
    tel = (config_data or {}).get("telemetry") or {}
    if not (tel.get("enabled") and str(tel.get("posthog_publishable") or "").strip()):
        return _CSP
    return (_CSP
            .replace("script-src 'self'", f"script-src 'self' {_POSTHOG_SCRIPT_HOST}")
            .replace("connect-src 'self' ws: wss:",
                     f"connect-src 'self' ws: wss: {_POSTHOG_CONNECT_HOSTS}"))


@app.middleware("http")
async def _csp_header(request, call_next):
    response = await call_next(request)
    # Computed per app start (lifespan). Fallback: the strict no-telemetry value.
    csp = getattr(request.app.state, "csp", None) or _CSP
    response.headers.setdefault("Content-Security-Policy", csp)
    return response


# The four gate-ending route suffixes — deliberately the same set
# `guard.py`'s `_GATE_PATH` regex already names (`approve`, `approve-landed`,
# `finish-review`, `shipped`), kept as a literal tuple here rather than
# imported from `guard.py` so this module has no import-time dependency on
# the CLI-hook module. `tests/test_gate_at_the_act.py` pins the two lists
# against each other so they cannot silently drift apart.
_GATE_ENDING_SUFFIXES = ("/approve", "/approve-landed", "/finish-review", "/shipped")


def _is_gate_ending_path(path: str) -> bool:
    """True for `/api/tasks/{id}/<gate-ending suffix>`. Interior repeated
    slashes, a trailing slash, `.`/`..` segments and one level of
    percent-encoding are normalized away first — the same class of
    normalization `guard.py`'s lexical route check applies.

    The two layers agree on every path Starlette actually routes to a
    gate-ending handler. They diverge on two spellings that Starlette does
    NOT route, so neither is a dodge: a doubled LEADING slash
    (`//api/tasks/x/approve` — `posixpath.normpath` preserves it, so this
    returns False where `guard.py` matches) and upper case
    (`/api/tasks/x/APPROVE` — `guard.py`'s regex is IGNORECASE, this
    comparison is not). Both reach the SPA catch-all instead of the approve
    handler and answer 405, measured; they cannot end the gate, so the
    stricter layer being the lexical one costs nothing here."""
    from urllib.parse import unquote

    normalized = posixpath.normpath(unquote(path))
    if not normalized.startswith("/api/tasks/"):
        return False
    return normalized.endswith(_GATE_ENDING_SUFFIXES)


@app.middleware("http")
async def _refuse_marked_gate_acts(request, call_next):
    """The act-level half of the human gate (session_mark.py), applied to
    the HTTP surface: a gate-ending route refuses a request that carries the
    agent-session mark — via `AGENT_SESSION_HEADER` (a marked CLI client
    sends it, see `cli/api_client.py`) or via this server process's own env
    mark — BEFORE the route handler runs, so no state mutates. Additive to
    the CLI-side `_refuse_agent_gate_act` and to `guard.py`'s existing
    lexical checks; this is the checkpoint that still catches a caller that
    reaches the API directly rather than through the `nh` CLI at all.

    Only POST is checked, because all four gate-ending routes are declared
    POST-only — a non-POST request cannot perform the act, and no GET route
    in this app matches `_is_gate_ending_path` at all (task detail is
    `/api/tasks/{id}`, the diff is `/api/tasks/{id}/diff`). The method test
    is what keeps the refusal off the OTHER traffic that does normalize onto
    those paths: dropping it turns the CORS preflight `OPTIONS
    /api/tasks/{id}/approve` into a 403 on a marked server (measured — the
    board's approve button would then fail as an opaque CORS error instead
    of the API's own JSON), and turns the SPA catch-all's 404 for a GET of
    the same path into a gate refusal. `tests/test_gate_at_the_act.py`
    (`test_non_post_methods_on_a_gate_path_are_not_gate_refused`,
    `test_every_gate_ending_route_is_post_only`) pins both halves.
    """
    if request.method.upper() == "POST" and _is_gate_ending_path(request.url.path):
        if request_is_marked(request.headers.get(AGENT_SESSION_HEADER)):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=403,
                content={
                    "error": "gate_refused",
                    "reason": (
                        "this request carries the agent-session mark; "
                        "gate-ending actions are operator-only "
                        "(see docs/security.md)."
                    ),
                },
            )
    return await call_next(request)


# The loopback boundary, outermost so it runs before every middleware above:
# every request must address 127.0.0.1/localhost/[::1] or the configured
# server.host, and a cross-origin browser write is refused (api/local_boundary.py).
install_local_boundary(app)


# --------------------------------------------------------------------------- #
# Connection manager for WebSocket broadcasts                                  #
# --------------------------------------------------------------------------- #

class _ConnMgr:
    """B2 #9: every socket has TWO writers (its ws_board poll loop and the
    mutation broadcasts) — unserialized interleaved send_text corrupted
    sockets that then died silently while the client still showed
    "Connected". A per-socket lock serializes all sends."""

    def __init__(self) -> None:
        self._sockets: list[WebSocket] = []
        self._locks: dict[int, asyncio.Lock] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._sockets.append(ws)
        self._locks[id(ws)] = asyncio.Lock()

    def remove(self, ws: WebSocket) -> None:
        if ws in self._sockets:
            self._sockets.remove(ws)
        self._locks.pop(id(ws), None)

    async def send(self, ws: WebSocket, text: str) -> None:
        lock = self._locks.get(id(ws))
        if lock is None:
            await ws.send_text(text)
            return
        async with lock:
            await ws.send_text(text)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload)
        dead: list[WebSocket] = []
        for sock in list(self._sockets):
            try:
                await self.send(sock, text)
            except Exception:  # noqa: BLE001
                dead.append(sock)
        for sock in dead:
            self.remove(sock)


_mgr = _ConnMgr()


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _store(req: Request) -> Store:
    return req.app.state.store


SETUP_MODE_DETAIL = (
    "no_human is in setup mode: no Claude credential is on file, so nothing "
    "that spends tokens can run. Finish auth setup — add "
    "CLAUDE_CODE_OAUTH_TOKEN to ~/.no_human/.env (chmod 600), created with "
    "`claude setup-token`, or run `nh init` — then reload the board."
)


def _require_credentials(request: Request) -> None:
    """Refuse a token-spending call while the server has no credential.

    Gated only for apps that opted in: `lifespan` always sets
    `app.state.setup_mode` at boot, so the real server is always covered. A
    test app that hand-builds `app.state` without touching that attribute
    (e.g. tests/test_api.py's `client` fixture, which predates this feature)
    is unchanged today — same as the bare `cfg is None` short-circuit this
    replaces. Re-probed per call once opted in (cheap: one env-file read) so
    adding the token lifts the restriction without a restart. Left ungated:
    onboarding, Settings, /api/version, /api/config, every read-only route.
    """
    state = request.app.state
    if not hasattr(state, "setup_mode"):
        return  # opted out (e.g. a bare test app): unchanged today
    cfg = getattr(state, "config", None)
    if cfg is None:
        return
    from ..config import subscription_credential_missing
    if subscription_credential_missing(cfg.data) is None:
        state.setup_mode = False
        return
    raise HTTPException(status_code=503, detail=SETUP_MODE_DETAIL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _require_task(store: Store, task_id: str) -> Task:
    task = await store.find_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
    return task


async def _emit_task_event(
    store: Store, task_id: str, kind: str, text: str, *, persist: bool = True,
) -> None:
    """Broadcast a merge-progress frame over the existing WebSocket, so the
    SlideOver's live-progress panel sees `merge_started`/`merge_step_*`
    within one server round-trip of the step actually happening.

    ``persist=False`` is for `human_merged`, which `set_status` already
    writes to `task_events` — broadcasting it again here (for a second
    observer/tab watching mid-merge) must not double-insert it."""
    ev = {"source": "human", "kind": kind, "text": text, "ts": time.time()}
    if persist:
        await store.save_events(task_id, [ev])
    await _mgr.broadcast({"type": "task_event", "task_id": task_id, "event": ev})


async def _refuse_approve(
    store: Store, task_id: str, reason: str, status: int, *, detail: Any = None,
) -> None:
    """Record the refusal so the drawer's history shows it, then raise.

    The board's approve button was failing silently (operator on task
    e24cee25/PR #643 saw nothing when containment refused the merge) because
    no refusal path wrote anything the UI could render or replay — this
    writes an `approve_refused` event with the exact refusal text before
    raising, so it survives closing/reopening the drawer."""
    await _emit_task_event(store, task_id, "approve_refused", reason)
    raise HTTPException(status_code=status, detail=detail if detail is not None else reason)


def _latest_pr_url(attempts: list[dict]) -> str | None:
    for a in reversed(attempts):
        if a.get("pr_url"):
            return a["pr_url"]
    return None


def _max_pr_conflict_rounds() -> int:
    """The configured wake-watcher bound (wake.py reads the same key with the
    same default), surfaced on summaries so the badge renders 'round N/M'."""
    cfg = getattr(app.state, "config", None)
    blockers = cfg.data.get("blockers") if cfg is not None else None
    if not isinstance(blockers, dict):
        # A malformed `blockers:` scalar in config.yaml must degrade to the
        # default, never AttributeError every board endpoint.
        blockers = {}
    try:
        return int(blockers.get("max_pr_conflict_rounds", 3))
    except (TypeError, ValueError):
        return 3


async def _board_tasks(
    store: Store, scheduler=None, *, limit: int | None = None, offset: int | None = None,
) -> list[TaskSummaryOut]:
    """`limit`/`offset` (P5) are threaded through ONLY by the `GET /api/tasks`
    route — every other caller here (approve/cancel/pause/..., `/ws`) needs
    the full board and calls this with the defaults, unchanged."""
    tasks = await store.list_tasks(limit=limit, offset=offset)
    # B2 #16: ONE grouped query instead of an N+1 per board tick per socket.
    by_task = await store.attempts_by_task()
    # SCRUM-15: `scheduler.inflight` returns a fresh set() copy per call — snapshot
    # once so every card in this response is judged against the same instant.
    inflight = scheduler.inflight if scheduler is not None else set()
    out = []
    for task in tasks:
        attempts = by_task.get(task.id, [])
        summary = TaskSummaryOut.from_task(
            task, _latest_pr_url(attempts), attempts=attempts,
            max_pr_conflict_rounds=_max_pr_conflict_rounds(),
        )
        if scheduler is not None:
            summary.claimed = task.id in inflight
            ls = scheduler.get_live_status(task.id)
            if ls:
                summary.live_status = ls
            # Subtask progress for compound parents.
            if task.status.value == "compound_parent":
                subs = await store.list_subtasks(task.id)
                if subs:
                    done = sum(1 for s in subs if s.status.value == "done")
                    summary.subtask_progress = f"{done}/{len(subs)}"
        # Lane last: it is decided from the fields above, and it is decided HERE
        # rather than in the frontend so every client reads one answer.
        summary.lane = lane_for(summary)
        out.append(summary)
    return out


def _git_diff(repo_path: str, commit_sha: str, base: str | None = None) -> str:
    try:
        diff_range = f"{commit_sha}~1..{commit_sha}"
        if base:
            # A recorded base can still be gone (branch deleted, worktree
            # pruned, base only ever existed on the remote) — verify it
            # resolves before trusting it, so a broken base fails soft into
            # the single-commit range instead of an empty diff.
            check = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
                cwd=repo_path, capture_output=True, text=True, timeout=10,
            )
            if check.returncode == 0:
                # Three-dot form diffs from the merge-base, so base moving on
                # after the branch was cut does not inject unrelated files.
                diff_range = f"{base}...{commit_sha}"
            else:
                log.info(
                    "commit_sha %s has an unresolvable base_branch %r; "
                    "falling back to single-commit diff %s — the board will "
                    "show only the last commit",
                    commit_sha, base, diff_range,
                )
        else:
            log.info(
                "commit_sha %s has no recorded base_branch; falling back to "
                "single-commit diff %s — the board will show only the last "
                "commit",
                commit_sha, diff_range,
            )
        proc = subprocess.run(
            ["git", "diff", diff_range, "--no-color"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        return proc.stdout[:32000] if proc.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# REST endpoints                                                               #
# --------------------------------------------------------------------------- #

def _sched(request: Request):
    return getattr(request.app.state, "scheduler", None)


# --------------------------------------------------------------------------- #
# feature_used — CLOSED vocabulary.                                            #
#                                                                              #
# telemetry.record("feature_used", name=...) carries exactly one prop, and its #
# value must come from THIS set and nowhere else. Never a title, path, ticket  #
# key, integration name, filename or any other operator/request-derived        #
# string: the prop is a fixed literal chosen at the call site, so no operator  #
# content can ever reach the wire through it. Adding a name here is a privacy  #
# decision — it is pinned by tests/test_feature_used_telemetry.py.             #
# --------------------------------------------------------------------------- #
FEATURE_BACKLOG_IMPORT = "backlog_import"
FEATURE_ATTACHMENT_ADDED = "attachment_added"
FEATURE_INTEGRATION_SAVED = "integration_saved"

FEATURE_NAMES = frozenset({
    FEATURE_BACKLOG_IMPORT,
    FEATURE_ATTACHMENT_ADDED,
    FEATURE_INTEGRATION_SAVED,
})

# Sources that count as a backlog import/start for feature_used purposes.
# NOT the same set as create_task's own source clamp — see the call site.
# "linear" is a real value the web client sends: web/src/App.jsx's backlog-seed
# effect sets `source: tracker` where `tracker` is "linear" for a Linear-origin
# ticket (App.jsx ~L907), and TaskComposer passes that straight through to this
# endpoint's `body.source` (TaskComposer.jsx ~L153, L404).
_BACKLOG_IMPORT_SOURCES = frozenset({"jira", "linear"})


def _record_feature_used(request: Request, name: str) -> None:
    """One `feature_used` emission. `name` MUST be a FEATURE_* constant."""
    from .. import telemetry as _telemetry
    cfg = getattr(request.app.state, "config", None)
    _telemetry.record("feature_used",
                      config=cfg.data if cfg is not None else {}, name=name)


@app.get("/api/tasks", response_model=list[TaskSummaryOut])
async def list_tasks(
    request: Request,
    merge_ready: bool | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
    # No upper cap on `offset` (review round 1): SQLite's `OFFSET` past the
    # end of the result set just returns zero rows, and its cost is bounded
    # by the table's own row count either way — no worse than the uncapped,
    # unpaginated default every caller already gets today. A cap would only
    # protect against a cost this endpoint already carries.
    offset: int | None = Query(default=None, ge=0),
) -> list[TaskSummaryOut]:
    """P5 (fleet finding 6468d631): 700+ rows on the fleet meant every caller
    here — the board's init/resync fetch, CLI `board()`/`ping()`, the desktop
    shell's `probe()` — paid to hydrate and serialize the WHOLE table, always.

    `?limit`/`?offset` are additive and OPT-IN: no params (every caller
    today) is byte-for-byte the old unpaginated, newest-first response. Chosen
    over an incremental `?updated_since` because the board's own fetch is
    reconciliation against a WS snapshot that is already full-state — a row
    that vanished (deleted, cancelled without touching `updated_at`) must
    disappear from this list the same way it disappears from that snapshot,
    and `updated_since` can only ever report what CHANGED, never what is
    simply gone (no tombstone feed exists to cover that). A `limit`/`offset`
    page carries no such risk: it is always a true slice of the current
    table, so a future paginated consumer can adopt it safely.

    Only this route's own `_board_tasks` call is paginated — every internal
    caller (approve/cancel/pause/..., the `/ws` loop) still needs the full
    board and is untouched.
    """
    tasks = await _board_tasks(
        _store(request), scheduler=_sched(request), limit=limit, offset=offset,
    )
    if merge_ready:
        # Truthy-only filter (?merge_ready=1): the merge-ready policy is
        # advisory ("this row is not ready" and "this row was never
        # evaluated" both read `merge_ready is not True`), so there is no
        # useful "?merge_ready=0" query to distinguish from the unfiltered
        # list — every other task already IS that.
        #
        # Applied AFTER the page is sliced: no live caller combines this with
        # `limit`/`offset` today (grep — 2026-09-01); a future one that does
        # can get fewer than `limit` rows back — documented, not solved.
        tasks = [t for t in tasks if t.merge_ready is True]
    return tasks


@app.post("/api/tasks", response_model=TaskSummaryOut, status_code=201)
async def create_task(body: CreateTaskRequest, request: Request) -> TaskSummaryOut:
    """Create a new task from the web board. The task is staged as PENDING and
    will be picked up by the next ``nh serve`` tick or ``nh watch``."""
    _require_credentials(request)
    store = _store(request)
    repo_path: str | None = None
    linked: list[str] = []
    # Resolve from project if given; project takes precedence over raw repo_path.
    if body.project_id:
        proj = await store.get_project(body.project_id)
        if not proj:
            raise HTTPException(404, f"project {body.project_id!r} not found")
        # If the caller also specified a repo_path that belongs to this project,
        # use it as the target instead of the primary.  This lets the UI's
        # "target repo" picker work for multi-repo projects.
        if body.repo_path and body.repo_path in proj.repo_paths:
            repo_path = body.repo_path
        else:
            repo_path = proj.primary_repo
        linked = [r for r in proj.repo_paths if r != repo_path]
    elif body.repo_path:
        repo = Path(body.repo_path).expanduser().resolve()
        if not repo.is_dir() or not (repo / ".git").exists():
            raise HTTPException(
                status_code=422,
                detail=f"repo_path {body.repo_path!r} is not a git repository",
            )
        repo_path = str(repo)
    # Closed allowlist of intake surfaces — anything else falls back to
    # "board" so an arbitrary client string never reaches Task.source.
    # "mcp" added for the MCP bridge (SCRUM-63): its tasks must stay
    # attributable, and jira sync already filters on source == "jira".
    source = body.source if body.source in ("board", "jira", "mcp") else "board"
    # A create is a backlog import/start when the client declares a tracker as
    # its origin. Read from the REQUEST, not the resolved `source` above: the
    # resolved value clamps "linear" to "board" (Task.source's own allowlist),
    # which would silently drop every Linear start. This reads only; it does
    # not widen what reaches Task.source.
    _is_backlog_import = body.source in _BACKLOG_IMPORT_SOURCES
    # Jira dedup key (SCRUM-32): only honored for source == "jira"; trim then
    # cap to 64 chars, exact-match only (no case/char normalization).
    external_id: str | None = None
    if source == "jira" and body.external_id is not None:
        external_id = body.external_id.strip()[:64] or None
    # Task 7: "Follow up" on a finished task — a sibling link, verified to exist
    # up front exactly like the project_id check above (404, never a silent
    # dangling reference). Never Task.parent_id: that is the compound-child
    # relation the orchestrator still schedules/aggregates on.
    if body.follows_id:
        followed = await store.get_task(body.follows_id)
        if not followed:
            raise HTTPException(404, f"task {body.follows_id!r} not found")
    task = Task.new(
        title=body.title,
        source=source,
        repo_path=repo_path,
        description=body.description,
        kind=body.kind,
        external_id=external_id,
        follows_id=body.follows_id or None,
    )
    try:
        task.priority = normalise_priority(body.priority)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    task.acceptance_criteria = body.acceptance_criteria
    task.linked_repos = linked
    # PR-001: honour an explicitly pinned base. Blank/whitespace is treated as
    # "not pinned" so an empty composer field cannot write an empty string that
    # would then beat the fallback (`ctx.get("base_branch") or
    # await self._implicit_base_branch(repo)` — "" is falsy, but an empty key is
    # still misleading to every reader and to the PR-time mismatch warning).
    pinned_base = (body.base_branch or "").strip()
    if pinned_base:
        task.context = {**(task.context or {}), "base_branch": pinned_base}
    # Carry the grill's intake-eval verdict (if the composer ran one) onto the
    # created task's context, exactly where the dispatch-time evaluator would
    # have written it for a bare create — this is what makes
    # `_act_on_stored_eval` reachable for grill-sourced tasks; see
    # CreateTaskRequest.eval_result. Pydantic already rejects a non-dict
    # `eval_result` (422) before this handler runs, so only "present and
    # truthy" needs checking here.
    if body.eval_result:
        task.context = {**(task.context or {}), "eval_result": body.eval_result}
    if body.backend:
        # Per-task coder backend (public issue #5) — set by API clients, and
        # by the board's composer, whose picker options come from GET
        # /api/config's `coder_backends` field (see show_config). Validated
        # against the one tuple `make_backend` accepts, so a typo is a 422
        # here instead of a BackendUnavailable on the first attempt.
        from ..agent.backend import SUPPORTED_BACKENDS
        chosen = body.backend.strip().lower()
        if chosen not in SUPPORTED_BACKENDS:
            raise HTTPException(
                422, f"unknown backend {body.backend!r}; one of "
                     f"{', '.join(SUPPORTED_BACKENDS)}")
        # A KNOWN backend this install cannot actually run (e.g. `local` with
        # no `llm.local_model`, or `codex` with no CLI/credential) is refused
        # HERE too, not just typo'd names above — a client that is not the
        # board's composer (whose own gating reads this same signal off GET
        # /api/config's `coder_backend_availability`) must not be able to file
        # a task that is guaranteed to die on its first coder turn. Same
        # preflight `core.runtime.build_orchestrator` runs at construction —
        # `describe_backend` turns its raise/no-raise into a 422/no-422 rather
        # than reimplementing it. Absent config (no `request.app.state.config`
        # yet) never blocks: that would be refusing on missing evidence, not
        # a real unavailability.
        cfg = getattr(request.app.state, "config", None)
        cfg_data = getattr(cfg, "data", None)
        if cfg_data is not None:
            from ..core.backend_settings import describe_backend
            info = await asyncio.to_thread(describe_backend, chosen, cfg_data)
            if not info["available"]:
                raise HTTPException(422, info["reason"])
        task.config["backend"] = chosen
    # GAP 1: opt in to the human plan-approval gate. Never for an imported
    # ticket — see CreateTaskRequest.plan_approval.
    if body.plan_approval and source != "jira":
        from ..core.plan_gate import CONFIG_KEY as _PLAN_APPROVAL_KEY
        task.config[_PLAN_APPROVAL_KEY] = True
    if repo_path:
        from ..profile import apply_default_task_config
        profile = await store.get_profile(repo_path)
        task.config = apply_default_task_config(profile, task.config)
    await store.create_task(task)
    if _is_backlog_import:
        _record_feature_used(request, FEATURE_BACKLOG_IMPORT)
    # Pre-flight feasibility HINT (feature #1): the free band (complexity tier +
    # the intake eval verdict — both knowable before planning) calibrated on
    # THIS install's own per-tier done-rate, stashed on the task for the drawer
    # to offer a 1-click split/clarify. Best-effort: a hint failure never
    # affects the create (estimate_feasibility is itself fail-open, and this
    # whole block is guarded).
    try:
        from ..core.feasibility import estimate_feasibility
        _cfg = getattr(request.app.state, "config", None)
        hint = estimate_feasibility(
            task, await store.done_rate_by_tier(),
            config=_cfg.data if _cfg is not None else None,
        )
        if hint is not None:
            task.context = await store.merge_context(task.id, {
                "feasibility_hint": {
                    "band": hint.band, "tier": hint.tier, "offer": hint.offer,
                    "done_rate_pct": hint.done_rate_pct,
                    "message": hint.message(),
                    # Hint-only families (e.g. multi_family) folded into
                    # `signals` by estimate_feasibility, plus their human
                    # -readable `hint_reasons` — never fed to band/offer above,
                    # but persisted so the pre-flight card can actually show
                    # them instead of computing them for nothing.
                    "signals": list(hint.signals),
                    "hint_reasons": list(hint.hint_reasons),
                },
            })
    except Exception:  # noqa: BLE001 — advisory; a hint never fails a create
        log.warning("feasibility hint at create failed for %s", task.id[:8])
    summary = TaskSummaryOut.from_task(
        task, max_pr_conflict_rounds=_max_pr_conflict_rounds())
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_created",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    return summary


@app.post("/api/tasks/{task_id}/split", response_model=list[TaskSummaryOut],
          status_code=201)
async def split_task(
    task_id: str, body: SplitRequest, request: Request,
) -> list[TaskSummaryOut]:
    """Split a PENDING over-scope task into 2-8 independent child tasks.

    A HUMAN api action (the board's split-review screen) — never a
    blocker-option verb, so the agent can never trigger a split. Each draft
    becomes its OWN task with ``parent_id`` set for provenance (the existing
    Sub-tasks view renders them), inheriting the parent's repo/project/backend/
    base. The children run independently through every gate — this does NOT
    revive ``compound_parent`` runtime coordination (removed 2026-08-12 on
    purpose). The original is cancelled, its scope now living in the children.

    Guarded on PENDING: a task already dispatched, parked or terminal has work
    in flight or done and must not be silently replaced.
    """
    _require_credentials(request)
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status != TaskStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=(f"can only split a pending task; this one is "
                    f"{task.status.value!r}"),
        )
    drafts = body.drafts
    if not (2 <= len(drafts) <= 8):
        raise HTTPException(
            422, "a split must create between 2 and 8 sub-tasks")
    for d in drafts:
        if not (d.title or "").strip():
            raise HTTPException(422, "each sub-task needs a non-empty title")

    # RESERVE the split before creating anything: flip the parent PENDING->FAILED
    # with the STATUS-GUARDED CAS (never human_override — that would clobber a
    # live claim). This closes a check-then-act race: the PENDING guard above is
    # at function entry, but every `await store.create_task(child)` yields the
    # event loop, and the scheduler (same loop) claims PENDING tasks. Creating
    # children first and force-cancelling last would let the scheduler start a
    # live orchestrator mid-loop, then get force-cancelled out from under it —
    # double execution + duplicate PRs. Two concurrent /split POSTs would
    # likewise both pass the entry guard and both create child sets. By moving
    # the parent off PENDING FIRST, the loser of either race sees the CAS refuse
    # (rowcount 0 -> None) and creates NO children.
    from ..blockers import human_event
    n = len(drafts)
    reason = f"split into {n} sub-tasks"
    prior_status = task.status
    task.context = await store.merge_context(task.id, {"cancel_reason": reason})
    moved = await store.set_status(
        task, TaskStatus.FAILED, validate=True, human_override=False,
        event=human_event("cancel", prior_status=prior_status,
                          prior_blocker=None, reason=reason,
                          actor="operator:api"),
    )
    if moved is None:
        # Lost the race: a scheduler tick claimed it, or a concurrent split
        # already moved it. Create nothing — the parent is no longer ours to
        # split. (task.status was re-synced to the live value by set_status.)
        raise HTTPException(
            status_code=409,
            detail=("task is no longer pending — it started running or was "
                    "already split"),
        )

    # Won the reservation: the parent is now terminally cancelled and NOT
    # claimable, so the children can be created without a live sibling.
    children: list[Task] = []
    for d in drafts:
        # Fold the proposer's contract into the description so the coder keeps
        # it (the child task has no contract field of its own).
        desc = (d.description or "").strip()
        contract = (d.contract or "").strip()
        if contract:
            desc = f"{desc}\n\nContract: {contract}".strip()
        child = Task.new(
            title=(d.title or "").strip(),
            source="board",
            repo_path=task.repo_path,
            description=desc or None,
            kind=task.kind,
            parent_id=task.id,
        )
        child.priority = task.priority
        child.acceptance_criteria = list(d.acceptance_criteria or [])
        child.linked_repos = list(task.linked_repos or [])
        # Inherit the parent's task config (coder backend, plan-approval gate…)
        # and any pinned base branch, so a split child runs exactly as the
        # parent would have.
        child.config = dict(task.config or {})
        base = (task.context or {}).get("base_branch")
        if base:
            child.context = {**(child.context or {}), "base_branch": base}
        await store.create_task(child)
        children.append(child)

    # Record the children on the cancel reason, for provenance.
    child_ids = ", ".join(c.id[:8] for c in children)
    await store.merge_context(
        task.id, {"cancel_reason": f"{reason}: {child_ids}"})

    out = [TaskSummaryOut.from_task(c, max_pr_conflict_rounds=_max_pr_conflict_rounds())
           for c in children]
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_split",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    return out


@app.get("/api/tasks/{task_id}/split-drafts")
async def get_split_drafts(task_id: str, request: Request) -> dict[str, Any]:
    """Generate 2-4 sub-task drafts for the split-review screen.

    LAZY: the single utility-model call runs only when the human opens the split
    screen, never at task creation — so a task nobody splits costs nothing.
    Returns ``{"drafts": [{title, description, contract}]}``, or ``{"drafts":
    []}`` when the proposer produced nothing parseable (the UI shows a
    "couldn't draft a split" state rather than an error).
    """
    _require_credentials(request)
    store = _store(request)
    task = await _require_task(store, task_id)
    # Only a PENDING task can actually BE split (POST /split enforces the same),
    # so refuse to spend a utility-model call drafting a split that could never
    # be applied to a running/parked/terminal task.
    if task.status != TaskStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=(f"only a pending task can be split; this one is "
                    f"{task.status.value!r}"),
        )
    from ..intake.split_proposal import generate_split_drafts
    files = ((task.context or {}).get("spec") or {}).get("files_to_change")
    drafts = await generate_split_drafts(task, files_to_change=files)
    return {"drafts": drafts or []}


async def _record_intake_spend(store, site: str, model: str | None, obj) -> None:
    """Book one intake call's tokens to the unattributed ledger, never raising.

    *obj* is any intake result carrying the three token fields (``GrillQuestion``
    / ``GrillResult`` / ``EvalResult``). Accounting is not allowed to break a
    request: a ledger write that fails degrades the record, not the intake.
    """
    if obj is None:
        return
    try:
        await store.record_unattributed_usage(
            site=site,
            model=model,
            tokens_used=getattr(obj, "tokens_used", 0),
            cache_read_tokens=getattr(obj, "cache_read_tokens", 0),
            cache_creation_tokens=getattr(obj, "cache_creation_tokens", 0),
        )
    except Exception as exc:  # noqa: BLE001 — accounting never blocks intake
        log.warning("intake usage not recorded for %s: %s", site, exc)


@app.post("/api/grill")
async def grill_step_endpoint(body: GrillStepRequest, request: Request):
    """B2: Run one step of the intake grill interrogation.

    Phase 4b changes:
      - Uses review_model (Sonnet) instead of primary_model (Opus) — the grill
        is read-only clarification; Sonnet is sufficient and cheaper.
      - Caches the backend in app.state._grill_sessions keyed by (title, repo)
        so multi-round grills reuse the same agent session (context carryover).
    """
    _require_credentials(request)
    from ..agent.claude_backend import ClaudeBackend
    from ..intake.grill import GrillQuestion, GrillResult, grill_step

    config = request.app.state.config
    store = _store(request)
    repo_path: str | None = None
    if body.project_id:
        proj = await store.get_project(body.project_id)
        if proj:
            if body.repo_path and body.repo_path in proj.repo_paths:
                repo_path = body.repo_path
            else:
                repo_path = proj.primary_repo
    elif body.repo_path:
        repo = Path(body.repo_path).expanduser().resolve()
        if not repo.is_dir() or not (repo / ".git").exists():
            raise HTTPException(
                status_code=422,
                detail=f"repo_path {body.repo_path!r} is not a git repository",
            )
        repo_path = str(repo)

    # Phase 4b: session reuse — cache grill backends by (title, repo).
    grill_sessions = getattr(request.app.state, "_grill_sessions", None)
    if grill_sessions is None:
        grill_sessions = {}
        request.app.state._grill_sessions = grill_sessions
    cache_key = (body.title, repo_path or "")
    backend = grill_sessions.get(cache_key)
    if backend is None:
        # Phase 4b: use review_model (Sonnet) for the grill subagent.
        backend = ClaudeBackend(
            model=config.review_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
            never_push_to=config["git"]["never_push_to"],
            readonly=True,
        )
        grill_sessions[cache_key] = backend
        # Evict oldest if cache grows (prevent unbounded memory).
        if len(grill_sessions) > 20:
            oldest = next(iter(grill_sessions))
            grill_sessions.pop(oldest, None)

    step = await grill_step(
        body.title, body.description, repo_path, body.qa_history, backend,
    )
    # This round's utility-tier spend. It cannot go on an attempt row: the
    # wizard runs before any task exists (and the operator may never finish
    # it), so it is booked to the unattributed intake ledger instead of being
    # forced onto some later task that did not ask for it.
    await _record_intake_spend(store, "api.grill", config.review_model, step)
    if isinstance(step, GrillResult):
        return GrillResultOut(
            title=step.title, description=step.description,
            acceptance_criteria=step.acceptance_criteria,
        )
    return GrillQuestionOut(
        question=step.question, suggestions=step.suggestions, round=step.round,
    )


@app.post("/api/grill/stream")
async def grill_stream_endpoint(body: GrillStepRequest, request: Request):
    """SSE endpoint — streams grill exploration events in real-time.

    Each SSE frame is a JSON object with {ts, kind, text, source}.
    The final frame carries kind="grill_result" or kind="grill_question"
    with the full payload. Falls through to the sync POST semantics on
    the backend — only the transport is different.
    """
    _require_credentials(request)
    from ..agent.claude_backend import ClaudeBackend
    from ..intake.grill import GrillQuestion, GrillResult, grill_step

    config = request.app.state.config
    store = _store(request)
    repo_path: str | None = None
    if body.project_id:
        proj = await store.get_project(body.project_id)
        if proj:
            if body.repo_path and body.repo_path in proj.repo_paths:
                repo_path = body.repo_path
            else:
                repo_path = proj.primary_repo
    elif body.repo_path:
        repo = Path(body.repo_path).expanduser().resolve()
        if not repo.is_dir() or not (repo / ".git").exists():
            raise HTTPException(
                status_code=422,
                detail=f"repo_path {body.repo_path!r} is not a git repository",
            )
        repo_path = str(repo)

    grill_sessions = getattr(request.app.state, "_grill_sessions", None)
    if grill_sessions is None:
        grill_sessions = {}
        request.app.state._grill_sessions = grill_sessions
    cache_key = (body.title, repo_path or "")
    backend = grill_sessions.get(cache_key)
    if backend is None:
        backend = ClaudeBackend(
            model=config.review_model,
            forbidden_paths=config["safety"]["forbidden_paths"],
            never_push_to=config["git"]["never_push_to"],
            readonly=True,
        )
        grill_sessions[cache_key] = backend
        if len(grill_sessions) > 20:
            oldest = next(iter(grill_sessions))
            grill_sessions.pop(oldest, None)

    queue: asyncio.Queue = asyncio.Queue()

    def _on_event(event):
        """Push agent events into the SSE queue."""
        kind = getattr(event, "kind", "") or ""
        tool = getattr(event, "tool_name", "") or ""
        inp = getattr(event, "tool_input", None) or {}
        text = ""
        if kind == "tool_use" and tool:
            text = _summarize_tool(tool, inp)
        elif kind in ("text", "assistant", "result"):
            text = (getattr(event, "text", "") or "").strip()[:300]
            if not text:
                return
        else:
            return
        frame = {"ts": time.time(), "kind": kind if kind != "tool_use" else "tool_use",
                 "text": text, "source": "grill"}
        queue.put_nowait(frame)

    async def _run_grill():
        try:
            step = await grill_step(
                body.title, body.description, repo_path,
                body.qa_history or [], backend, on_event=_on_event,
            )
            # Same unattributed-ledger booking as the sync endpoint above.
            await _record_intake_spend(
                store, "api.grill_stream", config.review_model, step)
            if isinstance(step, GrillResult):
                # D1/D9: run evaluator and emit verdict before grill_result.
                try:
                    from ..intake.evaluator import evaluate_spec
                    eval_result = await evaluate_spec(
                        step.title, step.description, step.acceptance_criteria,
                        model=config.utility_model,
                    )
                    await _record_intake_spend(
                        store, "api.grill_stream.evaluate_spec",
                        config.utility_model, eval_result)
                    if eval_result:
                        queue.put_nowait({
                            "kind": "eval_verdict", "source": "grill",
                            **eval_result.as_dict(),
                        })
                except Exception:  # noqa: BLE001 — advisory
                    pass
                queue.put_nowait({
                    "kind": "grill_result", "source": "grill",
                    "type": "done", "title": step.title,
                    "description": step.description,
                    "acceptance_criteria": step.acceptance_criteria,
                })
            else:
                queue.put_nowait({
                    "kind": "grill_question", "source": "grill",
                    "type": "question", "question": step.question,
                    "suggestions": step.suggestions, "round": step.round,
                })
        except Exception as exc:
            queue.put_nowait({"kind": "error", "text": str(exc), "source": "grill"})
        finally:
            queue.put_nowait(None)  # sentinel

    async def _generate():
        task = asyncio.create_task(_run_grill())
        try:
            while True:
                frame = await asyncio.wait_for(queue.get(), timeout=130)
                if frame is None:
                    yield "data: {\"kind\": \"done\", \"text\": \"stream ended\"}\n\n"
                    return
                yield f"data: {json.dumps(frame)}\n\n"
        except asyncio.TimeoutError:
            yield "data: {\"kind\": \"done\", \"text\": \"stream timeout\"}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, request: Request) -> TaskOut:
    store = _store(request)
    task = await _require_task(store, task_id)
    attempts = await store.list_attempts(task.id)
    out = TaskOut.from_task(task, attempts)
    # D1.3: the phase timeline (D1.1 rows) + ran-time. Empty until the
    # orchestrator writes rows (D1.2) — phases stays [] and active_seconds
    # stays null, which the drawer reads as "no ran chip" rather than "0s".
    out.phases = [PhaseOut.from_row(r) for r in await store.phases_for(task.id)]
    out.active_seconds = (await store.active_seconds(task.id)) or None
    # f22495d8: surface the SAME cost-weighted lifetime budget the
    # BUDGET_EXHAUSTED gate kills on (`Orchestrator._check_lifetime_budget`), so
    # a human sees how close a task is to being killed — the #1 real-failure
    # class. The EXACT helpers and args the gate uses, not a second estimate:
    # `weighted_tokens` over the INCLUDED classes from `lifetime_usage_by_class`
    # (infra/mechanical/dead-interrupted spend is excluded from the cap there),
    # and `Orchestrator._stored_token_cap` against the same config-driven bounds
    # default the gate reads in `_lifetime_limits`.
    _, by_class, _ = await store.lifetime_usage_by_class(task.id)
    cap = Orchestrator._stored_token_cap(
        task.config or {}, "lifetime_tokens",
        Bounds.from_config(request.app.state.config.data.get("bounds")).lifetime_tokens,
        task)
    from ..core.runtime import task_backend_override
    used = weighted_tokens(**by_class, backend=task_backend_override(task))
    out.budget = BudgetOut(used=used, cap=cap, remaining=cap - used)
    # SCRUM-16: same claimed contract as the board summaries (SCRUM-15) — the
    # slide-over must know whether a live session actually holds this task.
    sched = _sched(request)
    if sched is not None:
        out.claimed = task.id in sched.inflight
    return out


@app.get("/api/tasks/{task_id}/attempts/{attempt_number}/details",
         response_model=AttemptDetailsOut)
async def get_attempt_details(
    task_id: str, attempt_number: int, request: Request,
) -> AttemptDetailsOut:
    """The three heavy per-attempt blobs (`review_checklist`, `verifier_results`,
    `test_results`) `GET /api/tasks/{id}` no longer inlines (P1, running-task
    page slow-open) — multi-KB JSON per attempt, observed on live tasks, that
    made the detail payload large on every open/poll regardless of whether the
    drawer needed them yet. `web/src/api.js`'s `fetchTask` calls this once per
    attempt and merges the result back onto `AttemptOut`, so the drawer's own
    code never has to know the split happened.
    """
    store = _store(request)
    task = await _require_task(store, task_id)
    attempts = await store.list_attempts(task.id)
    for a in attempts:
        if a.get("attempt_number") == attempt_number:
            return AttemptDetailsOut.from_row(a)
    raise HTTPException(
        404, f"attempt {attempt_number} not found for task {task_id!r}")


@app.get("/api/tasks/{task_id}/subtasks", response_model=list[TaskSummaryOut])
async def list_subtasks(task_id: str, request: Request) -> list[TaskSummaryOut]:
    store = _store(request)
    subs = await store.list_subtasks(task_id)
    out = []
    for t in subs:
        attempts = await store.list_attempts(t.id)
        out.append(TaskSummaryOut.from_task(
            t, _latest_pr_url(attempts), attempts=attempts,
            max_pr_conflict_rounds=_max_pr_conflict_rounds()))
    return out


@app.get("/api/tasks/{task_id}/diff", response_class=PlainTextResponse)
async def get_diff(task_id: str, request: Request) -> str:
    store = _store(request)
    task = await _require_task(store, task_id)
    # For code_review tasks, the PR diff is stored in context.
    ctx = task.context or {}
    if ctx.get("pr_diff"):
        return ctx["pr_diff"]
    if not task.repo_path:
        return ""
    attempts = await store.list_attempts(task.id)
    base = (ctx.get("base_branch") or "").strip() or None
    for a in reversed(attempts):
        sha = a.get("commit_sha")
        if sha:
            return _git_diff(task.repo_path, sha, base)
    return ""


def _review_pass_evidence(context: dict, head_sha: str, repo) -> tuple[bool, str]:
    """(passed, evidence-line) for the branch's HEAD sha — mirrors the CLI's
    helper of the same name (`cli/commands.py`). Kept local rather than
    shared: `vcs/approve_merge.py` sits below `core/` (`core.orchestrator`
    already imports `vcs` at module scope) so it cannot import Orchestrator
    itself, and this endpoint already imports Orchestrator anyway."""
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


@app.post("/api/tasks/{task_id}/approve")
async def approve_task(task_id: str, request: Request) -> dict[str, Any]:
    """Approve and merge — squash-lands the PR under the operator identity.
    The agent itself still never merges anything on its own (constraint #2);
    this endpoint IS the human merge action `nh approve`/the GUI button
    trigger.

    Records `context.approved_at` and, in the SAME write, clears any stale
    `context.approval_superseded_at` from an EARLIER approval round on this
    task (e.g. one recorded, then escalated/sent-back, then re-approved after
    a fresh attempt) — this fresh approval is live again. A later escalation,
    conflict-round send-back, or new attempt re-stamps
    `approval_superseded_at` on its own (`core/db.py::_write_status`,
    triggered by the status leaving `awaiting_approval`); this endpoint never
    stamps it itself, only clears it. `approved_at` is never cleared here or
    anywhere — it stays a permanent audit trail; `core/lanes.py::
    approval_pending` is the one predicate that derives the "approved - merge
    pending" chip from both fields plus `status` together."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status != TaskStatus.AWAITING_APPROVAL:
        await _refuse_approve(
            store, task_id,
            f"task is {task.status.value!r}, not awaiting_approval",
            409,
        )
    # Idempotency guard (409, not the button's disabled state alone): a
    # second approve for the same task — a raced double-click that beat the
    # frontend's own disable, or a second browser tab — must never reach a
    # second `land_task` (a second squash/push race). Database-backed CAS on
    # `context.merge_in_progress` so this holds across server instances, not
    # just in-process.
    if not await store.claim_merge(task.id):
        await _refuse_approve(store, task_id, "Merge already in progress", 409)
    # Opt-in telemetry: the click itself, nothing about WHAT was approved.
    try:
        from .. import telemetry as _telemetry
        cfg = getattr(request.app.state, "config", None)
        _telemetry.record("approve_clicked",
                          config=cfg.data if cfg is not None else {})
    except Exception:
        pass
    try:
        task.context = await store.merge_context(
            task.id, {"approved_at": _now(), "approval_superseded_at": None})
        # An already-satisfied claim has no PR to merge — approval IS the human
        # confirmation its terminal promised, so it completes the task (the agent
        # still never merges anything; there is nothing to merge). Guarded on
        # pr_url: the report key persists in context, and after a send-back a
        # LATER attempt may ship a real PR — that approval must stay a merge
        # instruction, never a false DONE (PR #101 round-2 review).
        message = "Approval recorded. Merge the PR in your git host — the agent never merges."
        landed_sha = ""
        completed_landed = False

        loop = asyncio.get_running_loop()

        def on_step(step: str) -> None:
            # Called from the `land_task` worker thread (asyncio.to_thread) —
            # bridge back onto the event loop so the broadcast can await the
            # websocket sends. `_emit_task_event` never raises.
            asyncio.run_coroutine_threadsafe(
                _emit_task_event(store, task.id, f"merge_step_{step}", f"merge: {step}"),
                loop,
            )

        async def _merge(pr_url: str) -> tuple[str, dict[str, str] | None]:
            await _emit_task_event(store, task.id, "merge_started", "merge started")
            return await _merge_task_pr(request, store, task, pr_url, on_step=on_step)

        async def _fail_merge(error_detail: dict[str, str]) -> None:
            # `detail` keeps its `{step, stderr}` shape for `landFailureFeedback`
            # (frozen response contract); the event gets a readable one-liner.
            step = error_detail.get("step", "?")
            stderr = str(error_detail.get("stderr", ""))[:500]
            await _refuse_approve(
                store, task.id, f"merge failed at {step}: {stderr}", 500,
                detail=error_detail,
            )

        if (task.context or {}).get("already_satisfied_report"):
            # Guarded on `task_has_pr_evidence`, not `attempts.pr_url` alone (live
            # incident, task 8c8b36b5): a draft PR opened pre-review is recorded
            # only in `context["pr_draft_created"]` or a `pr_draft` event, never
            # on an attempt row — reading attempts alone missed it and completed
            # the task while its PR sat open.
            pr_url = await task_has_pr_evidence(store, task)
            if not pr_url:
                # No PR on record yet — decide (and, if the satisfying commit
                # is only on the task branch, act on) what this claim actually
                # requires via the SAME helper `nh approve` uses
                # (`vcs/task_pr.py::land_already_satisfied_claim`), so the two
                # surfaces can never diverge on this decision. Root-cause
                # incident this closes: a claim used to be marked DONE here
                # unconditionally, even when its satisfying commit was never
                # pushed to (or merged into) the base branch.
                from ..vcs.task_pr import land_already_satisfied_claim

                git_cfg = request.app.state.config["git"]
                step = await land_already_satisfied_claim(
                    store, task, repo_path=task.repo_path or "",
                    identity_name=git_cfg["agent_identity_name"],
                    identity_email=git_cfg["agent_identity_email"],
                    never_push_to=git_cfg["never_push_to"],
                    github_hosts=git_cfg.get("github_hosts"),
                )
                if step["decision"] == "refuse":
                    # NOT a hard failure: the approval itself stands (recorded
                    # above) — there is just nothing landed yet. The task
                    # stays awaiting_approval; a human (or a later approve
                    # retry) still has to resolve whatever `reason` names.
                    message = (
                        "Already satisfied claim confirmed, but could not be "
                        f"landed automatically: {step['reason']}. Task remains "
                        "awaiting_approval."
                    )
                elif step["decision"] == "done":
                    await store.set_status(
                        task, TaskStatus.DONE, validate=False,
                        event={"source": "human", "kind": "approved_already_satisfied",
                               "text": "already-satisfied claim confirmed by approve — "
                               + step["reason"],
                               "actor": process_actor()},
                    )
                    message = ("Already satisfied claim confirmed — no code change was "
                               "needed. Task done (there is no PR; the agent never merges).")
                else:
                    # decision == "land": the satisfying commit lives only on
                    # the task branch and IS the deliverable — a PR now
                    # exists for it (opened by the helper above if one didn't
                    # already). Fall through to the normal PR-merge path
                    # below, exactly like a task that shipped a real diff
                    # would; never mark this done without actually landing it
                    # (the incident this closes). Mirror the helper's own
                    # `pr_watch`/`pr_branch` write into this local copy of
                    # `task.context` — `resolve_task_pr` (inside `_merge`,
                    # via `_merge_task_pr`) reads `task.context` directly, not
                    # the store, so without this the freshly-opened PR would
                    # be invisible to it on this same request.
                    pr_url = step["pr_url"]
                    task.context = {**(task.context or {}), "pr_watch": pr_url,
                                     "pr_branch": step["branch"]}
                    landed_sha, error_detail = await _merge(pr_url)
                    if error_detail:
                        await _fail_merge(error_detail)
                    if landed_sha:
                        message = _merge_outcome_message(landed_sha)
                    else:
                        completed_landed, message = await _landed_completion_outcome(
                            store, task, landed_sha)
            else:
                landed_sha, error_detail = await _merge(pr_url)
                if error_detail:
                    await _fail_merge(error_detail)
                if landed_sha:
                    message = _merge_outcome_message(landed_sha)
                else:
                    completed_landed, message = await _landed_completion_outcome(
                        store, task, landed_sha)
        else:
            pr_url = await task_has_pr_evidence(store, task)
            if pr_url:
                landed_sha, error_detail = await _merge(pr_url)
                if error_detail:
                    await _fail_merge(error_detail)
                if landed_sha:
                    message = _merge_outcome_message(landed_sha)
                else:
                    completed_landed, message = await _landed_completion_outcome(
                        store, task, landed_sha)
        tasks = await _board_tasks(store, scheduler=_sched(request))
        await _mgr.broadcast({
            "type": "task_approved",
            "task_id": task.id,
            "tasks": [t.model_dump() for t in tasks],
        })
        if landed_sha or completed_landed:
            await _mgr.broadcast({
                "type": "task_updated", "task_id": task.id,
                "status": TaskStatus.DONE.value,
                "tasks": [t.model_dump() for t in tasks],
            })
        return {
            "ok": True,
            "message": message,
            "landed_sha": landed_sha,
        }
    finally:
        await store.release_merge(task.id)


@app.post("/api/tasks/{task_id}/approve-landed")
async def approve_landed(
    task_id: str, body: LandedOverrideRequest, request: Request,
) -> dict[str, Any]:
    """The HUMAN landed-override affirmation: a human asserts (with required
    justification) that a task's content landed at ``sha``, for any of four
    narrow shapes ``blockers/landed_override.py`` resolves and gates:

    - an ``awaiting_approval`` task where automated containment honestly
      refuses (a supervising session's squash train adapted the content: a
      later train car's classification-decision edits, or a real
      union-resolved source conflict, so no candidate commit's tree matches
      the branch verbatim), or
    - a ``failed`` task that died before ever opening a PR (budget
      exhaustion, a pre-review test failure, a compile error) whose content
      a human later hand-landed — refused if the task was human-cancelled or
      already has PR evidence (that pair goes through
      ``nh task restore-approval`` instead), or
    - a ``pending`` task that a human hand-lands before any coder attempt
      ever dispatched — refused if it already has PR evidence, same as above, or
    - a ``done`` task whose completion was real but whose event log carries
      none of ``vcs.task_pr.DONE_EVIDENCE_KINDS`` (so ``nh doctor`` reports it
      as an evidence gap forever) — refused if the task already carries one
      of those kinds, has a pending cancellation request, or still has PR
      evidence outstanding (same ``restore-approval`` pointer as above).

    See ``blockers/landed_override.py`` for the full contract; this endpoint
    only cheap-guards obviously-ineligible statuses and otherwise delegates
    every eligibility decision to that module. ``sha`` is checked against a
    list of candidate base branches (the project's default, the task's
    recorded base, and — narrowing to exactly itself when given — ``body.base``);
    the response's ``matched_branch`` names whichever one it matched.

    This is deliberately additive: a replay on an already-repaired task
    reaches ``blockers/landed_override.py``'s own standing-evidence check
    (the ``approved_landed_override`` event this endpoint just wrote is
    itself one of ``DONE_EVIDENCE_KINDS``) and 400s from there rather than
    409ing here, so a replay still cannot append a duplicate override event —
    it is just refused one layer deeper than for the other three shapes,
    because DONE is this shape's *starting* status, not only its ending one.
    It never merges, pushes, or touches git state — the override is a
    recorded human assertion, not a merge action (constraint #2: the agent
    never merges; there is nothing to merge here)."""
    from ..blockers.landed_override import OverrideRefused, approve_landed_override

    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status not in (
        TaskStatus.AWAITING_APPROVAL, TaskStatus.FAILED, TaskStatus.PENDING,
        TaskStatus.DONE,
    ):
        await _refuse_approve(
            store, task_id,
            (
                f"task is {task.status.value!r}, not awaiting_approval, "
                "a pre-PR failed task, a never-dispatched pending task, or "
                "a done task with no completion evidence on record"
            ),
            409,
        )
    try:
        result = await approve_landed_override(
            store, task, body.sha, body.justification, base=body.base)
    except OverrideRefused as exc:
        await _refuse_approve(store, task_id, exc.reason, 400)

    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_approved",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    await _mgr.broadcast({
        "type": "task_updated", "task_id": task.id,
        "status": TaskStatus.DONE.value,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "message": result["text"],
        "sha": result["sha"],
        "residue": result["residue"],
        "matched_branch": result.get("matched_branch"),
    }


async def _landed_completion_outcome(store, task, landed_sha: str) -> tuple[bool, str]:
    """After a no-op `_merge_task_pr` (``landed_sha == ""``), tell whether
    that no-op was the landed-completion path (`complete_if_approved_and_landed`
    already wrote DONE) rather than one of the existing skip/refusal paths
    (task still AWAITING_APPROVAL — no branch/repo recorded, an unresolvable
    branch, or `land_task` deciding it is disabled/has nothing to merge).
    Re-reads the row rather than threading a third return value through
    `_merge_task_pr`, since every existing "" -> no-op path there leaves the
    task AWAITING_APPROVAL and only this new path writes DONE."""
    assert not landed_sha
    refreshed = await store.get_task(task.id)
    if refreshed is not None and refreshed.status == TaskStatus.DONE:
        return True, ("Content is already on the default branch — approval "
                       "recorded and task completed; no merge was attempted.")
    return False, _merge_outcome_message(landed_sha)


def _merge_outcome_message(landed_sha: str) -> str:
    if landed_sha:
        return f"Approved and merged — landed {landed_sha[:12]} onto the default branch."
    return "Approval recorded. Merge the PR in your git host — the agent never merges."


async def _merge_task_pr(
    request: Request, store, task, pr_url: str,
    on_step: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, str] | None]:
    """Land the PR (vcs/approve_merge.land_task) off the event loop.

    Returns ``(landed_sha, error_detail)``. ``landed_sha`` is "" on any
    skip/refusal/failure. ``error_detail`` is ``None`` on a clean skip (no
    repo/branch to merge, `land_task` itself decided `approve_merge.enabled`
    is false, etc — approval stays recorded, today's record-only message
    stands) and is ``{"step": ..., "stderr": ...}`` on a genuine land
    failure or a failed review-PASS precondition — the caller turns that
    into an `HTTPException(500, ...)` so the failure is surfaced to the
    human rather than silently read as success (plan §3/4). The task is
    marked DONE by the caller only when a sha comes back."""
    config = request.app.state.config
    if not task.repo_path:
        return "", None
    from ..vcs.approve_merge import land_task
    from ..vcs.git import GitError, GitRepo
    from ..vcs.task_pr import resolve_task_pr

    resolved = await resolve_task_pr(store, task)
    branch = resolved.branch
    if not branch:
        return "", None

    from ..blockers.shipped import complete_if_approved_and_landed
    landed = await complete_if_approved_and_landed(store, task, pr_url, branch=branch)
    if landed is not None:
        # Content is already on the default branch (a closed-PR squash train,
        # most often) — the task is DONE (or was already terminal) and no
        # merge was ever attempted. `approve_task` re-reads the row to build
        # its message/broadcast rather than this function returning a third
        # value, since the existing "" == "no merge happened" callers all
        # stay correct either way.
        return "", None

    def _resolve_head() -> tuple[str, GitRepo | None]:
        try:
            repo = GitRepo(
                Path(task.repo_path),
                identity_name=config["git"]["agent_identity_name"],
                identity_email=config["git"]["agent_identity_email"],
                never_push_to=config["git"]["never_push_to"],
            )
            repo.fetch()
            ref = repo.resolve_commitish(branch)
            return (repo._run("rev-parse", ref) if ref else ""), repo
        except (GitError, OSError):
            return "", None

    head_sha, repo = await asyncio.to_thread(_resolve_head)
    if not head_sha or repo is None:
        return "", None
    passed, evidence = _review_pass_evidence(task.context or {}, head_sha, repo)
    if not passed:
        return "", {"step": "preconditions", "stderr": evidence}

    tested = (await store.latest_attempt_branch(task.id)).get("commit_sha") or ""
    result = await asyncio.to_thread(
        land_task,
        repo_path=task.repo_path, branch=branch, pr_url=pr_url,
        task_id=task.id, task_title=task.title, review_evidence=evidence,
        config=config.data, on_step=on_step, tested_commit_sha=tested,
    )
    if result.skipped:
        return "", None
    if not result.ok:
        await _emit_task_event(
            store, task.id, "merge_failed",
            f"merge failed at {result.step}: {result.stderr[:200]}",
        )
        return "", {"step": result.step, "stderr": result.stderr}
    await store.set_status(
        task, TaskStatus.DONE, validate=False,
        event={"source": "human", "kind": "human_merged",
               "sha": result.landed_sha, "text": result.message,
               "actor": process_actor()},
    )
    await _emit_task_event(
        store, task.id, "human_merged", result.message, persist=False,
    )
    return result.landed_sha, None


@app.post("/api/tasks/{task_id}/finish-review")
async def finish_review(task_id: str, request: Request) -> dict[str, Any]:
    """Mark a code-review task done — the human has posted the comments they
    want (all, some, or none) and is finished. A code_review has no PR of its own
    to merge, so it never auto-completes; without this it stays stuck in Review
    PR even after the human is done. This is the explicit 'I'm done' action."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status != TaskStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r}, not awaiting_approval",
        )
    drafts = (task.context or {}).get("draft_review_comments") or []
    posted = sum(1 for d in drafts if d.get("posted"))
    await store.set_status(
        task, TaskStatus.DONE, validate=False,
        event={"source": "human", "kind": "review_finished",
               "text": f"review finished — {posted}/{len(drafts)} comment(s) posted",
               "actor": process_actor()},
    )
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_updated",
        "task_id": task.id,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "posted": posted,
        "total": len(drafts),
        "message": f"Review finished — {posted}/{len(drafts)} comment(s) posted.",
    }


_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # 20MB — a screenshot/doc, not a dataset


@app.post("/api/tasks/{task_id}/attachments")
async def add_attachment(
    task_id: str, request: Request, file: UploadFile = File(...),
) -> dict[str, Any]:
    """Attach a screenshot/document to a task. Stored on disk under
    ~/.no_human/attachments/<task_id>/ (files, not SQLite blobs — lean stack);
    the path is recorded on task.context so the coder can READ it for context
    (a screenshot of the bug, a design doc, an error log)."""
    import re as _re

    from ..config import NO_HUMAN_HOME
    store = _store(request)
    task = await _require_task(store, task_id)
    data = await file.read()
    if len(data) > _ATTACHMENT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="attachment exceeds 20MB")
    # Sanitize the name — no path traversal, no separators.
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "attachment").name)
    dest_dir = NO_HUMAN_HOME / "attachments" / _re.sub(r"[^A-Za-z0-9_-]", "", task_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe
    dest.write_bytes(data)
    attachments = list((task.context or {}).get("attachments") or [])
    attachments.append({"name": safe, "path": str(dest)})
    task.context = await store.merge_context(task.id, {"attachments": attachments})
    _record_feature_used(request, FEATURE_ATTACHMENT_ADDED)
    return {"ok": True, "name": safe, "path": str(dest), "count": len(attachments)}


@app.post("/api/tasks/{task_id}/send-back")
async def send_back(
    task_id: str, body: SendBackRequest, request: Request
) -> dict[str, Any]:
    """Return the task to IMPLEMENTING for the next daemon run."""
    from ..core.budget_floor import check_budget_floor

    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status == TaskStatus.DONE:
        raise HTTPException(status_code=409, detail="task is already done")
    if task.status == TaskStatus.FAILED and (task.context or {}).get("cancel_reason"):
        raise HTTPException(status_code=409, detail="task is cancelled")
    budget_warning = await check_budget_floor(
        store, task,
        bounds=Bounds.from_config(
            (getattr(request.app.state, "config", None) or {}).get("bounds")))
    prior_status = task.status
    prior_blocker = task.blocker if isinstance(task.blocker, dict) else None
    _sent_back_at = _now()
    await store.append_context_list(
        task.id, "send_back_feedback",
        {"at": _sent_back_at, "message": body.message})
    # Mark the send-back pending — cleared at the next `attempt_start`, or
    # named in the blocker if a loop-head gate (budget/attempt ceiling)
    # refuses to start a round at all (`orchestrator._refuse_round`).
    from ..blockers import record_pending_send_back
    await record_pending_send_back(
        store, task, source="send_back", message=body.message,
        actor="operator:api", at=_sent_back_at)
    # A human pressing "Send back" IS the gate the zero-diff honesty check looks
    # for, so record that this re-entry is theirs. No checkpoint is involved
    # here, so the write CLEARS any recorded `sha`/`branch` rather than
    # relabelling a sha this human never chose — relabelling is what disarmed
    # the gate and credited the loop's own abandoned partial.
    from ..blockers import human_event, resume_provenance
    # A CLEAR is a clear however it is spelled: this writes `sha: None`, and
    # the orphan sweep reads a `resume_from` with no sha exactly as it reads no
    # `resume_from` at all — so without this the sweep would re-stamp the dead
    # attempt's sha over the human's decision, with MACHINE provenance.
    await store.close_open_attempts(task.id)
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(None, "human")})
    # Both human-stop signals go: a board-Paused task that is sent back must
    # not run while still stamped `blocker.human_stopped` ("stopped by you").
    task.blocker = None
    task.wake_check_at = None
    await store.update_task_columns(task)
    # Reset to IMPLEMENTING so the next `nh watch <id>` retries.
    # Re-entering the loop withdraws any pending board Pause, or the next
    # attempt would honour it on turn zero and park the task straight back
    # (same as the CLI twin: cli/commands.py `nh task retry` / `nh reply`).
    await store.clear_cancel_request(task.id)
    await store.set_status(
        task, TaskStatus.IMPLEMENTING, validate=False,
        event=human_event(
            "reject", prior_status=prior_status, prior_blocker=prior_blocker,
            reason=body.message, actor="operator:api"),
    )
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_updated",
        "task_id": task.id,
        "status": TaskStatus.IMPLEMENTING.value,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "message": "Feedback stored. Run `nh watch <id>` to retry.",
        "budget_warning": budget_warning.as_dict() if budget_warning else None,
    }


_PARKED_STATUSES = {
    TaskStatus.BLOCKED, TaskStatus.AWAITING_INPUT,
    TaskStatus.PAUSED_QUOTA, TaskStatus.ESCALATED,
}

_ACTIVE_STATUSES = {
    TaskStatus.CONTEXT, TaskStatus.PLANNING, TaskStatus.IMPLEMENTING,
    TaskStatus.REVIEWING, TaskStatus.TESTING,
}


# ESCALATED is the state a task is in when it is asking a human to decide —
# exactly the one a human most needs to be able to hold, same as a
# supervisor reserving the quota window (SCRUM-58).
_HOLDABLE_STATUSES = {TaskStatus.PAUSED_QUOTA, TaskStatus.BLOCKED, TaskStatus.ESCALATED}


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(
    task_id: str, request: Request,
) -> dict[str, Any]:
    """Pause a running task (sets to BLOCKED with reason). For a task already
    parked (paused_quota/blocked) — e.g. a supervisor reserving the quota
    window — this instead stamps a durable human hold (blocker.human_stopped)
    on the existing blocker without touching status; the wake sweep already
    skips human_stopped tasks (SCRUM-22)."""
    from ..blockers import BlockerCategory

    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status in _HOLDABLE_STATUSES:
        existing = task.blocker or {}
        if existing.get("category") == BlockerCategory.USER_PAUSED.value:
            # A PAUSE and a HOLD are mutually exclusive stop shapes (see
            # taxonomy.py's module docstring): a PAUSE already resumes in one
            # step and the wake sweep already treats it correctly at
            # max_park. Stamping `human_stopped` on top would make it a HOLD
            # too — never swept, resumed only by releasing the hold — so
            # `nh task resume` / `POST /resume` would no longer be the whole
            # story. Refuse, idempotently: it is already paused.
            return {"ok": True, "message": f"Already paused {task_id[:8]}"}
        blocker_data = dict(existing)
        blocker_data.setdefault("category", BlockerCategory.USER_PAUSED.value)
        blocker_data.setdefault("question", "Paused from board")
        blocker_data["human_stopped"] = True
        task.blocker = blocker_data
        await store.update_task_columns(task)
        tasks = await _board_tasks(store, scheduler=_sched(request))
        await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                              "tasks": [t.model_dump() for t in tasks]})
        return {"ok": True, "message": f"Held {task_id[:8]}"}
    if task.status not in _ACTIVE_STATUSES and task.status != TaskStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r} — only active tasks can be paused",
        )
    # A task the scheduler is RUNNING is owned by its worker: raise the cancel
    # flag — the only signal a live orchestrator observes — and let it stop at
    # its next cooperative checkpoint, commit the tree as [WIP-BLOCKED] and
    # park itself (`Orchestrator._honor_cancel`). That is exactly what
    # `nh task pause` does (`cli/commands.py`). Writing BLOCKED from here
    # instead flipped the status under a worker that kept running, and the
    # pause recorded NO checkpoint — so a later resume branched from base and
    # the attempt's work was thrown away. Found by the 2026-08-20 sweep of
    # every blocker writer for a missing `resume_commit`.
    # Raise the flag FIRST, before the inflight check, so the two branches
    # below differ only in who parks: the worker (flag left for it) or this
    # handler (flag withdrawn again). A worker this server cannot see
    # (`nh watch`/`nh serve` in another process is invisible to
    # `scheduler.inflight`) is NOT covered here — the direct-park branch
    # withdraws the flag before such a worker's 3s poll would see it; the CLI
    # covers that case by probing the server, not `inflight`.
    await store.request_cancel(task.id, "Paused from board")
    sched = _sched(request)
    if sched is not None and task.id in getattr(sched, "inflight", set()):
        tasks = await _board_tasks(store, scheduler=sched)
        await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                              "tasks": [t.model_dump() for t in tasks]})
        # Honoured at the attempt's next cooperative checkpoint — the coder
        # session's next tool call, or the top of the next attempt when the
        # pause lands during planning/review/testing.
        return {"ok": True,
                "message": (f"pause requested {task_id[:8]} — the running "
                            "attempt will stop at its next checkpoint")}
    # Nothing this server is running: park it directly, and withdraw the flag
    # so a later retry/resume does not re-park on turn zero.
    # Carry the checkpoint the task already had (a crashed worker's park is
    # exactly where it is worth keeping) — `carried_checkpoint` honours a
    # human's sha-less `resume_from` as a veto, like `_honor_cancel`.
    from ..blockers import carried_checkpoint, human_event, user_pause_blocker
    prior_status = task.status
    prior_blocker = task.blocker if isinstance(task.blocker, dict) else None
    prior = carried_checkpoint(task) or {}
    task.blocker = user_pause_blocker(
        "Paused by operator via web board", checkpoint=prior, paused_by="board")
    await store.update_task_columns(task)
    await store.set_status(
        task, TaskStatus.BLOCKED, validate=False,
        event=human_event(
            "pause", prior_status=prior_status, prior_blocker=prior_blocker,
            actor="operator:api"),
    )
    await store.clear_cancel_request(task.id)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Paused {task_id[:8]}"}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(
    task_id: str, request: Request,
) -> dict[str, Any]:
    """Resume a paused/blocked/escalated task (sets to IMPLEMENTING). If the
    task carries a durable human hold (blocker.human_stopped, set by /pause
    on an already-parked task), this only clears that flag — the task stays
    in its current parked status so the wake sweep can decide the next
    transition, rather than resume forcing one."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status not in _PARKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r} — only parked tasks can be resumed",
        )
    prior_status = task.status
    prior_blocker = task.blocker if isinstance(task.blocker, dict) else None
    if isinstance(task.blocker, dict) and task.blocker.get("human_stopped"):
        blocker_data = dict(task.blocker)
        del blocker_data["human_stopped"]
        task.blocker = blocker_data
        await store.update_task_columns(task)
        tasks = await _board_tasks(store, scheduler=_sched(request))
        await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                              "tasks": [t.model_dump() for t in tasks]})
        return {"ok": True, "message": f"Released hold on {task_id[:8]}"}
    # Read the checkpoint BEFORE clearing the blocker, which is what holds the
    # sha — exactly as `nh task resume` does. This endpoint is the Resume button
    # in the drawer, and it used to do neither: it dropped the blocker on the
    # floor, so the next attempt branched from a stale `resume_from` (or from
    # base) and silently threw away everything the parked attempt had already
    # committed, and it left the previous actor's `by` describing a resume a
    # human had just performed. Two independent reviews found this same hole.
    from ..blockers import human_event, resume_checkpoint, resume_provenance
    checkpoint = resume_checkpoint(task.blocker)
    task.blocker = None
    task.wake_check_at = None
    await store.update_task_columns(task)
    task.context = await store.merge_context(
        task.id, {"resume_from": resume_provenance(checkpoint, "human")})
    # Re-entering the loop withdraws any pending board Pause, or the next
    # attempt would honour it on turn zero and park the task straight back
    # (same as the CLI twin: cli/commands.py `nh task retry` / `nh reply`).
    await store.clear_cancel_request(task.id)
    await store.set_status(
        task, TaskStatus.IMPLEMENTING, validate=False,
        event=human_event(
            "resume", prior_status=prior_status, prior_blocker=prior_blocker,
            actor="operator:api"),
    )
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Resumed {task_id[:8]} → implementing"}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, request: Request, body: CancelRequest | None = None,
) -> dict[str, Any]:
    """Cancel a task (sets to FAILED). `body` is optional so the CLI's and
    the board's pre-existing no-reason POST keep working unchanged; when the
    board's cancel modal supplies a typed reason it is trimmed and truncated
    to 500 chars before being recorded, matching the client-side clamp."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status in {TaskStatus.DONE, TaskStatus.FAILED}:
        raise HTTPException(
            status_code=409,
            detail=f"task is already {task.status.value!r}",
        )
    from ..blockers import human_event
    prior_status = task.status
    prior_blocker = task.blocker if isinstance(task.blocker, dict) else None
    reason = (body.reason if body else None) or ""
    reason = reason.strip()[:500] or "Cancelled from board"
    task.context = await store.record_cancel_reason(task.id, reason)
    # Nothing can honour a pending board Pause after the kill below — withdraw
    # it, as `nh task cancel` does (cli/commands.py).
    await store.clear_cancel_request(task.id)
    await store.set_status(
        task, TaskStatus.FAILED, validate=False, human_override=True,
        event=human_event(
            "cancel", prior_status=prior_status, prior_blocker=prior_blocker,
            reason=reason, actor="operator:api"),
    )
    # Cancel must STOP the work, not just flip the status. First: the reliable
    # path. If THIS server's scheduler owns a live orchestrator for this task,
    # `request_task_cancel` cancels the asyncio.Task wrapping the coder's
    # `backend.run(...)` directly — the existing attempt-timeout unwind then
    # closes the attempt row with its checkpoint and true spend. This does
    # NOT depend on the task id appearing in any process's argv, which is the
    # defect being fixed here: the Agent SDK's bundled `claude` process
    # carries no task id, so the pkill below alone matched nothing and left
    # it running for the rest of the attempt.
    sched = _sched(request)
    stopped = bool(sched is not None and sched.request_task_cancel(task.id, reason))
    # Best-effort fallback for anything pkill CAN see (e.g. a pytest
    # subprocess spawned under the worktree) — kept unconditionally since it
    # is harmless when nothing matches, but it is no longer the primary
    # signal `cancel_stopped_session`/`cancel_session_not_found` is based on:
    # pkill's own exit code cannot distinguish "matched and killed" from "no
    # match" (`_kill_task_processes` treats both as success), so it cannot
    # honestly report what it stopped.
    await _kill_task_processes(task.id)
    # `source="orchestrator"` (not `_emit_task_event`'s hardcoded "human"):
    # this reports what the SYSTEM did in response to the human's cancel, a
    # distinct fact from the human_cancel event above — conflating the two
    # under source="human" broke the one-human-event-per-verb invariant
    # `test_every_board_endpoint_emits_its_shared_human_event` guards.
    stop_kind = "cancel_stopped_session" if stopped else "cancel_session_not_found"
    stop_text = (
        f"cancel stopped the running coder session for task {task_id[:8]}"
        if stopped else
        f"no live in-process coder session found for task {task_id[:8]}"
    )
    stop_ev = {"source": "orchestrator", "kind": stop_kind, "text": stop_text,
               "ts": time.time()}
    await store.save_events(task.id, [stop_ev])
    await _mgr.broadcast({"type": "task_event", "task_id": task.id, "event": stop_ev})
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Cancelled {task_id[:8]}"}


def _windows_kill_by_cmdline(task_id: str) -> int:
    """Windows equivalent of ``pkill -9 -f <task_id>``. Returns 1 if it ran.

    Windows has NO built-in kill-by-command-line, so this is two steps rather
    than one, and the choice between the candidates matters:

    * ``wmic`` would do it in one call, but it is deprecated and REMOVED from
      Windows 11 24H2 onward — a cleanup that silently stops working on new
      machines is the same class of defect as the ``pkill`` that was never
      there.
    * ``taskkill`` can only match an image name or a PID, never a command line,
      so on its own it cannot find a task's children at all.

    So: enumerate PIDs with PowerShell over ``Win32_Process.CommandLine``
    (present on every supported Windows), then ``taskkill /F /T`` each one.
    ``/T`` also takes the process TREE, which is what "the task's SDK and
    pytest subprocesses" actually means and which ``pkill -f`` only achieved
    because each child carried the id in its own argv.

    UNTESTED ON WINDOWS — no Windows host was available. What is tested here is
    the argv shape, the self-exclusion, and that the branch is taken at all.
    """
    # The id is interpolated into a PowerShell string, so it must not be able
    # to carry quoting. Task ids are 32-hex; anything else is refused rather
    # than escaped, because an escaping bug here is a command injection.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        log.warning("cancel: refusing to match on a non-alphanumeric task id")
        return 0
    # Both this PowerShell and our own process carry the id in their command
    # lines, so both are excluded — otherwise the cleanup kills the server.
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-CimInstance Win32_Process | Where-Object { "
        + f"$_.CommandLine -like '*{task_id}*' -and $_.ProcessId -ne $PID "
        + f"-and $_.ProcessId -ne {os.getpid()} "
        + "} | ForEach-Object { $_.ProcessId }"
    )
    enum = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=20,
    )
    if enum.returncode != 0:
        return 0
    for line in (enum.stdout or "").split():
        if not line.isdigit():
            continue
        subprocess.run(["taskkill", "/F", "/T", "/PID", line],
                       capture_output=True, timeout=10)
    return 1


async def _kill_task_processes(task_id: str) -> int:
    """Best-effort kill of a task's worktree subprocesses (SDK + pytest) by its
    unique id. Returns how many pkill patterns matched (for tests/telemetry)."""
    if not task_id or len(task_id) < 12:  # never pkill on a too-broad pattern
        return 0
    try:
        if _IS_WINDOWS:
            return await asyncio.to_thread(_windows_kill_by_cmdline, task_id)
        proc = await asyncio.to_thread(
            subprocess.run, ["pkill", "-9", "-f", task_id],
            capture_output=True, timeout=10,
        )
        return 1 if proc.returncode in (0, 1) else 0  # 1 = no match, still fine
    except Exception:  # noqa: BLE001
        log.debug("cancel: worktree process cleanup best-effort failed", exc_info=True)
        return 0


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(
    task_id: str, request: Request,
) -> dict[str, Any]:
    """Retry a failed task (resets to PENDING for a fresh run)."""
    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status != TaskStatus.FAILED:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r} — only failed tasks can be retried",
        )
    from ..blockers import human_event
    prior_status = task.status
    prior_blocker = task.blocker if isinstance(task.blocker, dict) else None
    task.blocker = None
    task.wake_check_at = None
    # None deletes the key (RFC 7396) — clears cancel_reason atomically.
    #
    # `resume_from` is cleared for the same reason, and it is load-bearing: this
    # endpoint promises "a fresh run", and a fresh run must not silently branch
    # from a checkpoint some EARLIER actor chose. Leaving it behind meant the
    # zero-diff honesty gate judged the retry by a decision nobody made for it —
    # and if that stale pair carried `by: "human"`, the gate was disarmed for a
    # run no human had gated. Retry means from base; a human who wants to
    # continue from a checkpoint has Resume for that.
    # And the attempt rows the dead worker left `in_progress` are retired with
    # it. The orphan sweep re-derives a checkpoint from exactly those rows, so
    # clearing the context alone let the next sweep undo this endpoint's whole
    # promise — see `Store.close_open_attempts`.
    await store.close_open_attempts(task.id)
    task.context = await store.merge_context(
        task.id, {"cancel_reason": None, "retried_at": _now(),
                  "resume_from": None})
    await store.update_task_columns(task)
    # Re-entering the loop withdraws any pending board Pause, or the next
    # attempt would honour it on turn zero and park the task straight back
    # (same as the CLI twin: cli/commands.py `nh task retry` / `nh reply`).
    await store.clear_cancel_request(task.id)
    await store.set_status(
        task, TaskStatus.PENDING, validate=False, human_override=True,
        event=human_event(
            "retry", prior_status=prior_status, prior_blocker=prior_blocker,
            actor="operator:api"),
    )
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({"type": "task_updated", "task_id": task.id,
                          "tasks": [t.model_dump() for t in tasks]})
    return {"ok": True, "message": f"Retried {task_id[:8]} → pending"}


@app.post("/api/tasks/{task_id}/shipped", response_model=TaskOut)
async def mark_shipped(
    task_id: str, body: ShippedRequest, request: Request,
) -> TaskOut:
    """Record that a human (the supervising session) merged this task's work
    outside no_human — e.g. a squash-merge done by hand after review.

    Non-goal (SCRUM-55 post-mortem): ``sha`` is recorded as operator
    testimony ONLY. It is never fetched, resolved against a ref, or verified
    against any git remote — SCRUM-55 built exactly that verification and
    burned its whole budget on a stale-trunk check plus an unrunnable commit
    test. This endpoint deliberately does not re-add it: the supervising
    human is the trust anchor here, not git.
    """
    store = _store(request)
    task = await _require_task(store, task_id)

    if task.status == TaskStatus.DONE:
        raise HTTPException(status_code=409, detail="task is already done")
    if task.status == TaskStatus.FAILED and (task.context or {}).get("cancel_reason"):
        raise HTTPException(status_code=409, detail="task is cancelled")
    # _SHIPPABLE allow-list removed — operator-testimony model: the
    # supervising human is the trust anchor, so shipped is valid from any
    # non-terminal status (SCRUM-69). done and cancelled remain 409 above.

    sha = body.sha.strip()
    if not sha:
        raise HTTPException(status_code=400, detail="sha must not be empty")

    task.blocker = None
    task.wake_check_at = None
    await store.update_task_columns(task)
    await store.set_status(
        task, TaskStatus.DONE, validate=False, human_override=True,
        event={"source": "human", "kind": "human_merged",
               "sha": sha, "note": body.note, "ts": time.time(),
               "actor": process_actor()},
    )
    attempts = await store.list_attempts(task.id)
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_updated", "task_id": task.id,
        "status": TaskStatus.DONE.value,
        "tasks": [t.model_dump() for t in tasks],
    })
    return TaskOut.from_task(task, attempts)


class PostReviewCommentsRequest(BaseModel):
    items: list[int] | None = None  # indices of items to post; None = all failed


def _parse_pr_url(url: str) -> tuple[str, str, str, int]:
    """Parse a GHE/GitHub PR URL → (hostname, owner, repo, pr_number)."""
    import re
    m = re.match(
        r"https?://([^/]+)/([^/]+)/([^/]+)/pull/(\d+)",
        url,
    )
    if not m:
        raise ValueError(f"cannot parse PR URL: {url}")
    return m.group(1), m.group(2), m.group(3), int(m.group(4))


@app.post("/api/tasks/{task_id}/post-review-comments")
async def post_review_comments(
    task_id: str, body: PostReviewCommentsRequest, request: Request,
) -> dict[str, Any]:
    """Post review comments to the PR via gh api on behalf of the human."""
    store = _store(request)
    task = await _require_task(store, task_id)

    ctx = task.context or {}
    pr_url = ctx.get("pr_url")            # anchor / fallback for unmatched files
    pr_files = ctx.get("pr_files") or {}  # {url: [files]} — routes each finding to its PR/MR
    if not pr_url and not pr_files:
        raise HTTPException(400, "no PR URL stored for this task")

    # Get the checklist from the latest attempt.
    attempts = await store.list_attempts(task.id)
    checklist = None
    for a in reversed(attempts):
        cl = a.get("review_checklist")
        if cl:
            checklist = json.loads(cl) if isinstance(cl, str) else cl
            break
    if not checklist or not checklist.get("items"):
        raise HTTPException(400, "no review checklist found")

    items = checklist["items"]
    if body.items is not None:
        indices = [i for i in body.items if 0 <= i < len(items)]
    else:
        indices = [i for i, it in enumerate(items) if not it.get("passed")]
    if not indices:
        return {"ok": True, "posted": 0, "results": []}

    # Route each finding to the change set that owns its file, and post via that
    # forge's API — a cross-repo review spans GitHub Enterprise AND GitLab, so a
    # a finding on a GitLab-hosted file must land on that MR (glab), not the GHE PR (gh).
    from ..vcs.comment_poster import pick_pr_for_file, post_to_pr

    results = []
    for idx in indices:
        item = items[idx]
        file_path = item.get("file", "") or ""
        line = item.get("line", 0) or 0
        comment = item.get("comment") or item.get("evidence", "")
        if not comment:
            results.append({"index": idx, "ok": False, "error": "no comment text"})
            continue
        target = pick_pr_for_file(file_path, pr_files, pr_url)
        if not target:
            results.append({"index": idx, "ok": False, "error": "no PR to post to"})
            continue
        res = await asyncio.to_thread(
            post_to_pr, target, comment, file_path or None, line if line > 0 else None,
        )
        entry = {"index": idx, "ok": res["ok"], "pr": target, "mode": res.get("mode")}
        if not res["ok"]:
            entry["error"] = res.get("error", "")[:300]
        results.append(entry)

    posted = sum(1 for r in results if r["ok"])
    return {"ok": posted > 0, "posted": posted, "total": len(indices), "results": results}


@app.get("/api/profiles")
async def list_profiles(request: Request) -> list[dict[str, Any]]:
    """Return onboarded repo profiles (for the New Task repo dropdown).

    When no profiles exist, falls back to distinct repo_paths from existing
    tasks so the repo picker has something to show.
    """
    store = _store(request)
    try:
        rows = await store.list_profiles()
    except Exception:  # noqa: BLE001 — table may not exist yet
        rows = []
    if rows:
        # `proven`/`is_usable` ride along so a caller can tell "onboarded" from
        # "has a test command the review gate can actually run" — the two the
        # board's chip used to conflate.
        from ..profile import ProjectProfile
        out_rows: list[dict[str, Any]] = []
        for r in rows:
            repo_path = r.get("repo_path", "") or ""
            row = {"repo_path": repo_path,
                   "ecosystem": r.get("ecosystem", ""),
                   "confirmed": bool(r.get("confirmed", False)),
                   "name": repo_path.rstrip("/").rsplit("/", 1)[-1] if repo_path else "",
                   "proven": {}, "test_proven": False, "is_usable": False,
                   "test_cmd": ""}
            if r.get("data"):
                try:
                    row.update(_profile_readiness(
                        ProjectProfile.from_dict(json.loads(r["data"]))))
                except (ValueError, TypeError) as exc:
                    log.warning("unreadable profile row for %s: %s", repo_path, exc)
            out_rows.append(row)
        return out_rows
    # Fallback: unique repo_paths from existing tasks.
    tasks = await store.list_tasks()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for t in tasks:
        rp = t.repo_path
        if rp and rp not in seen:
            seen.add(rp)
            out.append({"repo_path": rp, "ecosystem": "", "confirmed": False,
                        "name": rp.rstrip("/").rsplit("/", 1)[-1]})
    return out


async def _registered_repo_paths(request: Request) -> list[str]:
    """The operator's registered repo profiles, as the option list for the
    integrations "Run tasks in repo" select and its server-side membership
    check. Onboarded profiles ONLY (never task repo_paths) — a pulled-in ticket
    must run in a repo the operator deliberately set up, not any path a task
    once referenced. Empty (never raises) when the table does not exist yet."""
    store = _store(request)
    try:
        rows = await store.list_profiles()
    except Exception:  # noqa: BLE001 — table may not exist yet
        return []
    return [rp for r in rows if (rp := (r.get("repo_path") or ""))]


async def _known_repo_paths(store) -> set[str]:
    """Every repo the operator already knows: onboarded profiles + any repo a
    task references. This set is the allow-list for /api/repo — it is why the
    endpoint can render a repo map without ever walking an arbitrary path the
    caller supplies (which would leak any filesystem tree)."""
    known: set[str] = set()
    try:
        for r in await store.list_profiles():
            rp = (r.get("repo_path") or "").rstrip("/")
            if rp:
                known.add(rp)
    except Exception:  # noqa: BLE001 — table may not exist yet
        pass
    for t in await store.list_tasks():
        if t.repo_path:
            known.add(t.repo_path.rstrip("/"))
    return known


@app.get("/api/repos")
async def api_repos(request: Request) -> list[dict[str, Any]]:
    """The repos the operator knows (for the repo-understanding picker)."""
    store = _store(request)
    out = []
    for rp in sorted(await _known_repo_paths(store)):
        out.append({"repo_path": rp, "name": rp.rsplit("/", 1)[-1] or rp})
    return out


@app.get("/api/repo")
async def api_repo_understanding(request: Request, path: str) -> dict[str, Any]:
    """What no_human understands about ONE known repo: its onboarded profile,
    the cached repo map, and matched playbooks. Read-only. The path MUST be a
    repo the operator already onboarded or has a task for — an unknown path is
    a 404, never a fresh map of an arbitrary directory."""
    store = _store(request)
    norm = (path or "").rstrip("/")
    if not norm or norm not in await _known_repo_paths(store):
        raise HTTPException(status_code=404, detail="unknown repo")
    from ..context.repo_map import repo_map as _repo_map
    from pathlib import Path as _Path
    prof = await store.get_profile(norm)
    playbooks = await store.list_playbooks(project=norm)
    # repo_map walks the tree + shells out to git — offload it so a large or
    # stale-mount repo can never block the single-threaded event loop (the same
    # asyncio.to_thread discipline the rest of this file uses for blocking work).
    rmap = await asyncio.to_thread(_repo_map, _Path(norm))
    return {
        "repo_path": norm,
        "name": norm.rsplit("/", 1)[-1] or norm,
        "profile": prof.to_dict() if prof else None,
        "repo_map": rmap,
        "playbooks": [
            {"title": p.get("title", ""), "procedure": p.get("procedure", ""),
             "project": p.get("project")}
            for p in playbooks
        ],
    }


@app.get("/api/search")
async def api_search(request: Request, q: str, limit: int = 30) -> list[dict[str, Any]]:
    """Cross-task full-text search over the failure/fix record (events_fts,
    migration 0006 — attempt_failed / review / blocked / tamper / pr_ci_red /
    escalated / ci_gate_fail). "How was a failure like this handled before?"
    surfaced to the operator. Advisory: hostile FTS5 input (bare operators,
    unbalanced quotes) returns [], never a 500 — mirrors _recall_failures."""
    store = _store(request)
    terms = [t for t in (q or "").split() if t]
    if not terms:
        return []
    # FTS5 treats bare punctuation as operators; quote each term so user text is
    # matched literally and a stray `"`/`*`/`NEAR(` can't form a bad query.
    query = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
    lim = max(1, min(int(limit or 30), 30))
    try:
        rows = await store.query(
            """SELECT te.task_id,
                      json_extract(te.data, '$.kind'),
                      snippet(events_fts, 0, '', '', '…', 12),
                      te.ts
               FROM events_fts f
               JOIN task_events te ON te.id = f.rowid
               WHERE events_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, lim),
        )
    except Exception:  # noqa: BLE001 — search is advisory, never a 500
        return []
    if not rows:
        return []  # skip the O(all tasks) title scan on an empty result
    # Resolve task titles once, falling back to the id when a task row was since
    # deleted — a dangling fts row must never 500 the endpoint.
    titles: dict[str, str] = {t.id: t.title for t in await store.list_tasks()}
    return [
        {
            # Full id (the client truncates for display): grouping on a truncated
            # id could merge two tasks sharing an 8-char prefix.
            "task_id": str(task_id),
            "task_title": titles.get(str(task_id), str(task_id)[:8]),
            "kind": kind or "event",
            "snippet": snip or "",
        }
        for task_id, kind, snip, ts in rows
    ]


# --------------------------------------------------------------------------- #
# Projects — multi-repo grouping                                              #
# --------------------------------------------------------------------------- #

@app.get("/api/projects")
async def api_list_projects(request: Request) -> list[ProjectOut]:
    store = _store(request)
    projects = await store.list_projects()
    return [ProjectOut.from_project(p) for p in projects]


@app.post("/api/projects", response_model=ProjectOut, status_code=201)
async def api_create_project(
    body: CreateProjectRequest, request: Request
) -> ProjectOut:
    store = _store(request)
    # Validate repo paths.
    for rp in body.repo_paths:
        p = Path(rp).expanduser().resolve()
        if not p.is_dir() or not (p / ".git").exists():
            raise HTTPException(422, f"{rp!r} is not a git repository")
    repo_paths = [str(Path(rp).expanduser().resolve()) for rp in body.repo_paths]
    primary = None
    if body.primary_repo:
        primary = str(Path(body.primary_repo).expanduser().resolve())
    elif repo_paths:
        primary = repo_paths[0]
    from ..project_model import Project
    proj = Project.new(name=body.name, repo_paths=repo_paths, primary_repo=primary)
    try:
        await store.create_project(proj)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, f"project {body.name!r} already exists")
        raise
    return ProjectOut.from_project(proj)


@app.get("/api/projects/{project_id}")
async def api_get_project(project_id: str, request: Request) -> ProjectOut:
    store = _store(request)
    proj = await store.get_project(project_id)
    if not proj:
        raise HTTPException(404, "project not found")
    return ProjectOut.from_project(proj)


# What the regex does: allows 1-80 characters drawn only from ASCII letters,
# digits, dot, underscore and hyphen - no separators, no whitespace, nothing
# a shell or path expands. Two shapes that still match the charset are refused
# by explicit checks below: dots-only names ("." ".." "....") and ".git".
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

# The ONLY variables a git child of this endpoint inherits. An allowlist, not
# a denylist: git reads dozens of environment redirects, and a
# subtract-the-known-bad set lets through every name nobody enumerated.
# GIT_OBJECT_DIRECTORY (objects land outside $HOME while the call still
# answers 201) and GIT_COMMON_DIR (refs/config relocated outside $HOME) both
# slipped a scrub list that already named GIT_DIR and GIT_WORK_TREE.
# Everything the -c flags set (identity, gpgsign) is likewise unreachable from
# the environment here, since GIT_AUTHOR_*/GIT_COMMITTER_* outrank `-c user.*`.
#
# Measured on macOS with `env -i`: `git init`, `git add` and `git commit`
# all exit 0 with nothing but PATH and HOME set. The rest of this set is for
# behavior the operator would notice, not for git to run at all.
_GIT_ENV_KEEP = frozenset({
    "PATH",                        # finding the git binary
    "HOME",                        # git's own home lookups; ~/.gitconfig is
                                   # read but every value we care about is
                                   # pinned by a `-c` flag below
    "TMPDIR",                      # where git writes its temp files
    "LANG", "LC_ALL", "LC_CTYPE",  # message and path encoding
    "TZ",                          # timezone offset stamped on the commit
    # Windows equivalents. Git for Windows resolves `~` from USERPROFILE (or
    # HOMEDRIVE+HOMEPATH) and NOT from HOME, so with only "HOME" on this list
    # the sanitised env had no home at all there and ~/.gitconfig — identity,
    # credential helper, core.autocrlf — was silently never read. SystemRoot
    # and COMSPEC are needed for a process to start at all on Windows; PATHEXT
    # is how the loader finds `git.exe` from the bare name.
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "TEMP", "TMP", "SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT",
})


class ScaffoldRepoRequest(BaseModel):
    parent: str
    name: str


@app.post("/api/repos/scaffold", status_code=201)
async def scaffold_repo(body: ScaffoldRepoRequest, request: Request) -> dict[str, Any]:
    """Create a brand-new git repo and register it as a project.

    The composer's "create a new repo" affordance: mkdir + `git init` + a
    minimal README committed under the AGENT identity (the history must say
    plainly which commits a machine wrote), then the same registration path
    POST /api/projects uses, so the composer proceeds in it immediately.
    """
    # This endpoint writes to the operator's filesystem, so it takes the same
    # posture as the credential routes (a step beyond the read-mostly project
    # siblings): a cross-origin or origin-less browser write is refused, or a
    # drive-by page could litter $HOME with directories while `nh serve` is up.
    require_local_origin(request, writing=True)
    store = _store(request)
    config = request.app.state.config

    # NOT stripped: a name with any whitespace (including a trailing \n) is
    # rejected by the regex rather than silently laundered into a valid one.
    name = body.name or ""
    if not _REPO_NAME_RE.fullmatch(name):
        raise HTTPException(
            400, "invalid repo name - use letters, digits, '.', '_' or '-' "
                 "(1-80 characters)")
    # "." ".." "...." match the charset regex but are path navigation, not names.
    if set(name) == {"."}:
        raise HTTPException(
            400, "invalid repo name - a dots-only name is path navigation, "
                 "not a name")
    # Case-insensitive: the operator's filesystem usually is too, and a ".git"
    # directory is git's own metadata, not a repo.
    if name.lower() == ".git":
        raise HTTPException(
            400, "invalid repo name - '.git' is git's metadata directory")
    raw_parent = (body.parent or "").strip()
    parent = Path(raw_parent).expanduser()
    if not parent.is_absolute():
        raise HTTPException(400, "parent must be an absolute path")
    # resolve() BEFORE the containment check: a lexically-under-home path can
    # ..-escape it, and a symlinked segment can point anywhere.
    parent = parent.resolve()
    home = Path.home().resolve()
    if parent != home and home not in parent.parents:
        raise HTTPException(400, "parent must be a directory under your home")
    if not parent.is_dir():
        raise HTTPException(400, f"parent is not an existing directory: {parent}")
    target = parent / name
    if target.exists():
        raise HTTPException(409, f"{target} already exists")
    # The project is registered under the repo's name, and project names are
    # unique - so ~/a/dup and ~/b/dup cannot both be registered here. Refuse
    # the second one BEFORE anything is written: a 409 raised after the mkdir
    # leaves a real repo on disk that this endpoint can never register, since
    # the retry stops at the target.exists() check above.
    if await store.get_project_by_name(name):
        raise HTTPException(
            409, f"project {name!r} already exists - pick a different name "
                 f"(nothing was created)")

    git_cfg = config.data.get("git") or {}
    ident_name = git_cfg.get("agent_identity_name", "no_human")
    ident_email = git_cfg.get("agent_identity_email", "no-human@acme.com")

    # True only once THIS request's mkdir succeeded: the error-path cleanup is
    # gated on it, so a dir another writer made in the exists()->mkdir window
    # (mkdir raises FileExistsError) is never deleted as "our" debris.
    created = False

    def _scaffold() -> None:
        nonlocal created
        # Build the child env from _GIT_ENV_KEEP rather than copying
        # os.environ: the server's environment may carry any of git's write
        # redirects or identity overrides, and only names on that list reach
        # the child.
        git_env = {k: v for k, v in os.environ.items() if k in _GIT_ENV_KEEP}
        # With env= passed, exec resolves the binary against THIS PATH; if the
        # server started without one, fall back to the platform default rather
        # than searching an empty path.
        git_env.setdefault("PATH", os.defpath)
        git_env["GIT_CONFIG_NOSYSTEM"] = "1"

        def _git(*args: str) -> None:
            subprocess.run(  # no shell - argv only, nothing interpolated
                ["git", "-C", str(target),
                 "-c", f"user.name={ident_name}",
                 "-c", f"user.email={ident_email}",
                 # The operator's global config must not leak into a machine
                 # commit (gpg signing would block; templates would pollute).
                 "-c", "commit.gpgsign=false",
                 *args],
                check=True, capture_output=True, text=True, timeout=30,
                env=git_env)
        target.mkdir()
        created = True
        _git("init", "-q")
        (target / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        _git("add", "README.md")
        # --no-verify: hooks installed by an init template must not run here.
        _git("commit", "-q", "--no-verify", "-m", f"scaffold {name}")

    try:
        await asyncio.to_thread(_scaffold)
    except Exception as exc:
        # Never leak a stack trace; do log it, and remove the half-made dir so
        # a retry is not an instant 409 on our own debris - but ONLY if this
        # request made the dir (see `created`; never delete another writer's).
        log.warning("repo scaffold failed for %s: %s", target, exc)
        if created:
            import shutil
            shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(500, "creating the repository failed - see server logs")

    from ..project_model import Project
    proj = Project.new(name=name, repo_paths=[str(target)],
                       primary_repo=str(target))
    try:
        await store.create_project(proj)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            # The pre-check above cannot close the window: another writer can
            # take the name between it and this INSERT. Same answer, and the
            # directory this request made goes with it - otherwise the loser
            # of the race is left with the orphan the pre-check exists to
            # prevent.
            if created:
                import shutil
                shutil.rmtree(target, ignore_errors=True)
            raise HTTPException(
                409, f"project {name!r} already exists - pick a different name "
                     f"(nothing was created)")
        raise
    return {"repo_path": str(target), "project_id": proj.id}


def _auth_status_payload(request: Request) -> dict[str, Any]:
    """Which subscription pays, and whether a token is on file.

    Names and booleans ONLY — a token value is never returned (constraint §8),
    so this is safe to render in Settings. ``metered_key_present`` is surfaced
    because a stray ANTHROPIC_API_KEY silently bills the metered API, and a
    human should be able to see that without reading their shell profile.
    It is READ here, never scrubbed: a GET must not mutate the environment.
    """
    from ..config import (
        AuthError,
        active_auth_profile,
        available_auth_profiles,
        profile_token_var,
    )
    from ..config import _read_env_file as _env_file
    cfg = getattr(request.app.state, "config", None)
    data = getattr(cfg, "data", None) or {}
    configured = str((data.get("llm") or {}).get("auth_profile") or "default")
    # The effective billing mode this install is configured for. Default
    # matches config.py's own default and every other consumer (commands.py,
    # backend_check.py) — an install that never set llm.auth_mode is OAuth.
    auth_mode = str((data.get("llm") or {}).get("auth_mode") or "subscription")
    profiles = available_auth_profiles()
    running = active_auth_profile()
    try:
        token_var = profile_token_var(configured)
    except AuthError as exc:
        # A malformed llm.auth_profile on disk must not 500 a GET.
        token_var = f"(invalid profile in config: {exc})"
        # ...and it must not be ECHOED either. This branch already knows the
        # value was REJECTED — and one rejection reason is "that looks like a
        # token, not a profile name". The write guards stop new bad values, but
        # a hand-edited or legacy config.yaml would still render a pasted
        # secret straight into the Settings UI, and into any screenshot or bug
        # report made from it. Constraint §8: names and booleans only.
        configured = "(invalid — redacted)"
    # BOTH sources: a key sitting in .env is invisible to os.environ until the
    # next start, and "see it without reading your shell profile" is the
    # entire point of surfacing this. Shared by metered_key_present and
    # api_key_present so the two can never disagree.
    metered_key_present = bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or _env_file().get("ANTHROPIC_API_KEY"))
    if auth_mode == "api_key":
        # The active billing path IS "api_key" once the server has started
        # under this mode (_assert_api_key_mode stamps _ACTIVE_AUTH_PROFILE =
        # "api_key"): a restart would not change what pays, so it is not
        # required. Anything else running (an OAuth profile, or nothing yet)
        # means a restart is what actually switches billing to the key.
        restart_required = running != "api_key"
    else:
        # What config says pays vs what THIS process actually exported: a
        # long-lived server keeps billing the profile it started with, so
        # reporting only the config value would be a lie.
        restart_required = bool(running and running != configured)
    return {
        "configured_profile": configured,
        "active_profile": running,
        "restart_required": restart_required,
        "token_var": token_var,
        # Always "is an OAuth token on file for the configured profile" —
        # unchanged by auth_mode. In api_key mode a False here is expected
        # and not an error: billing runs on the key, not this token.
        "token_present": configured in profiles,
        "profiles": [{"name": p, "token_present": True} for p in profiles],
        "metered_key_present": metered_key_present,
        # The effective billing mode (frontend ticket renders it).
        "auth_mode": auth_mode,
        # Whether ANTHROPIC_API_KEY resolves at all (env or .env) — same
        # expression as metered_key_present, named for what it means in
        # api_key mode: the credential the BYO-key path bills with.
        "api_key_present": metered_key_present,
        # A token on file is necessary but not sufficient: the Claude Agent SDK
        # shells out to the `claude` CLI for every task. Without it the board
        # loads green and every task fails at launch — surface it here so
        # Settings can warn instead of letting the operator discover it one
        # failed task at a time.
        "backend_cli_present": _backend_cli_present(),
        # The Codex coding backend, made first-class in Settings alongside
        # Claude: its auth mode, credential presence and model. None (rather
        # than an absent key) when the on-disk config can't be read, so the
        # frontend degrades the same way an older server with no key at all does.
        "codex": _codex_status_payload(data),
    }


def _codex_status_payload(running_data: dict[str, Any]) -> dict[str, Any] | None:
    """The Codex sub-object of the auth status. Names and booleans ONLY — no
    OpenAI credential value is ever read into this, in EITHER mode (constraint
    §8 and constraint #6b: in subscription mode no_human holds no OpenAI
    credential and only existence-checks a live session via the CLI).

    ``auth_mode``/``model`` come from the ON-DISK config — what the operator
    just saved and the next task will use — not ``running_data``'s stale bound
    copy, so a value written by ``PUT /api/auth/codex-mode`` shows immediately.
    ``restart_required`` is the same file-vs-process comparison the Claude
    payload and ``/api/models`` do. Returns None on any failure so the Claude
    status GET can never be 500'd by a Codex-subsystem problem.
    """
    try:
        from ..config import (
            CODEX_API_KEY_VAR,
            CONFIG_PATH,
            AuthError,
            codex_auth_mode,
            load_config,
        )
        from ..config import _read_env_file as _env_file
        from ..agent.backend import default_codex_model

        def _mode(d: dict[str, Any]) -> str | None:
            try:
                return codex_auth_mode(d)
            except AuthError:
                # A malformed llm.codex_auth_mode on disk must not 500 a GET
                # (mirrors the Claude payload's invalid-profile handling). The
                # value is a mode word, never a secret, but None keeps the
                # contract "names and booleans only" and lets the UI show "—".
                return None

        on_disk = load_config(CONFIG_PATH).data
        mode = _mode(on_disk)
        running_mode = _mode(running_data)
        # BOTH sources — a key in .env is invisible to os.environ until the next
        # start — exactly like the Claude payload's metered_key_present.
        api_key_present = bool(
            os.environ.get(CODEX_API_KEY_VAR) or _env_file().get(CODEX_API_KEY_VAR))
        # subscription_session_present: only meaningful in subscription mode.
        # None means "not applicable / could not determine" — in api_key mode
        # (no session concept), or when the codex CLI is not resolvable so the
        # existence check cannot run. NEVER calls `codex login`, only the
        # non-raising `codex login status` probe.
        subscription_session_present: bool | None = None
        if mode == "subscription":
            from ..agent.codex_backend import codex_login_status, find_codex_cli
            if find_codex_cli() is not None:
                st = codex_login_status()
                # via == "api_key" is a key-backed session, not the ChatGPT plan
                # this mode wants — treat it as "not signed in" for this field.
                subscription_session_present = bool(
                    st.present and st.via != "api_key")
        model = str(
            (on_disk.get("llm") or {}).get("codex_model")
            or default_codex_model(mode or "api_key"))
        return {
            "auth_mode": mode,
            "api_key_present": api_key_present,
            "subscription_session_present": subscription_session_present,
            "model": model,
            "restart_required": bool(
                running_mode is not None and running_mode != mode),
        }
    except Exception:  # noqa: BLE001 — a Codex problem must not break Claude status
        return None


def _backend_cli_present() -> bool:
    """Whether the `claude` CLI the coding backend needs is resolvable.

    Read-only path resolution mirroring the SDK; never spawns the CLI.
    """
    from ..agent.backend_check import find_claude_cli

    return find_claude_cli() is not None


@app.get("/api/auth/status")
async def api_auth_status(request: Request) -> dict[str, Any]:
    require_local_origin(request)
    # Offloaded because the codex block can shell out to `codex login status`
    # (up to a 10s timeout) in subscription mode — the same reason /api/models
    # runs its payload in a thread. In the default api_key mode this is a pure
    # in-memory read and the thread hop is negligible.
    return await asyncio.to_thread(_auth_status_payload, request)


@app.put("/api/auth/token")
async def api_set_auth_token(request: Request) -> dict[str, Any]:
    """Store an OAuth token for a profile. Returns the same shape as status.

    The body is parsed BY HAND rather than through a pydantic model, because a
    pydantic validation error echoes the offending body back verbatim —
    submitting the form with the profile box empty returned
    ``{"input": {"token": "<the real secret>"}}`` in a 422. Constraint §8 says a
    secret is never echoed, and that has to hold for the failure paths too.

    This writes a CLAUDE_CODE_OAUTH_TOKEN[_PROFILE] — a subscription or
    enterprise OAuth token. It is NOT a bring-your-own-API-key path: a metered
    key is refused by `set_profile_token`, per constraint #1.
    """
    from ..config import (
        AuthError,
        SUBSCRIPTION_TOKEN_VAR,
        active_auth_profile,
        profile_token_var,
        set_profile_token,
    )

    require_local_origin(request, writing=True)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — never surface the raw body
        raise HTTPException(422, "expected a JSON object") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "expected a JSON object")
    profile, token = body.get("profile"), body.get("token")
    # Shapes only — never the values.
    if not isinstance(profile, str) or not isinstance(token, str):
        raise HTTPException(
            422, "both 'profile' and 'token' are required and must be strings")

    try:
        written = set_profile_token(profile, token)
    except AuthError as exc:
        # AuthError messages are written to be human-facing and never contain
        # the token; surfacing one is what makes the Settings form usable.
        raise HTTPException(422, str(exc)) from exc

    payload = await asyncio.to_thread(_auth_status_payload, request)
    # A restart is needed whenever the RUNNING process is still exporting a
    # different value for the profile it is billing — which is exactly what
    # rotating the active profile's token does, and the case the name-only
    # comparison in the status payload reports as False.
    running = active_auth_profile()
    with contextlib.suppress(AuthError):
        if running and written == profile_token_var(running):
            if os.environ.get(SUBSCRIPTION_TOKEN_VAR) != token:
                payload["restart_required"] = True
    return payload


@app.put("/api/auth/codex-mode")
async def api_set_codex_mode(request: Request) -> dict[str, Any]:
    """Set ``llm.codex_auth_mode`` (``"api_key"`` | ``"subscription"``).

    The MODE may live in config.yaml (constraint #6b); this writes it through
    the same config-splice discipline ``/api/config/models`` and
    ``/api/config/coder-backend`` use (``config.set_codex_auth_mode`` ->
    ``_splice_llm_scalar``, preserving the operator's comments), never a
    hand-rolled YAML edit. The body is parsed by hand so a malformed request
    gets one short operator-facing sentence, not pydantic's error tree. No
    credential is read or written here — only a mode word.
    """
    from ..config import (
        CODEX_AUTH_MODES,
        CONFIG_PATH,
        AuthError,
        set_codex_auth_mode,
    )

    require_local_origin(request, writing=True)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "expected a JSON object") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "expected a JSON object")
    mode = body.get("mode")
    if not isinstance(mode, str) or mode.strip().lower() not in CODEX_AUTH_MODES:
        raise HTTPException(
            422, f"'mode' must be one of {sorted(CODEX_AUTH_MODES)}")
    try:
        await asyncio.to_thread(
            set_codex_auth_mode, mode.strip().lower(), CONFIG_PATH)
    except (ValueError, AuthError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return await asyncio.to_thread(_auth_status_payload, request)


@app.put("/api/auth/codex-key")
async def api_set_codex_key(request: Request) -> dict[str, Any]:
    """Store the Codex backend's API key in ``~/.no_human/.env`` (never config.yaml).

    The Codex twin of ``PUT /api/auth/token``: a `.env`-only credential write
    through ``config.set_codex_api_key`` (``upsert_env_var``, chmod 600). The
    body is parsed BY HAND so a pydantic 422 can never echo the key back
    (constraint §8), and the response returns the VARIABLE NAME only — the key
    value is never returned, logged or echoed. Allowed here ONLY because it is
    an .env write; the key never reaches config.
    """
    from ..config import AuthError, set_codex_api_key

    require_local_origin(request, writing=True)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — never surface the raw body
        raise HTTPException(422, "expected a JSON object") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "expected a JSON object")
    key = body.get("key")
    # Shape only — never the value.
    if not isinstance(key, str):
        raise HTTPException(422, "'key' is required and must be a string")
    try:
        written = await asyncio.to_thread(set_codex_api_key, key)
    except AuthError as exc:
        # AuthError messages are written to be human-facing and never contain
        # the key.
        raise HTTPException(422, str(exc)) from exc

    payload = await asyncio.to_thread(_auth_status_payload, request)
    payload["codex_key_var"] = written
    return payload
def _bench_payload(card: "NorthStarCard", refusals: list[str]) -> dict[str, Any]:
    """The wire shape of a bench card. One function so the healthy and the
    unreadable path cannot answer with different keys."""
    agg = card.as_dict()["aggregate"]
    return {
        "label": card.label,
        "created_at": card.created_at,
        **agg,
        # With no gated specs the rate is a CONVENTION (1.0), not a
        # measurement, and the UI renders a bare "100% — denominator unknown".
        # That is the unreadable 100% the numerator/denominator pair exists to
        # kill, one layer down. None renders as "—".
        "honest_escalation_rate": (agg["honest_escalation_rate"]
                                   if agg["escalation_specs"] else None),
        "refusals": refusals,
        "override_reasons": card.override_reasons,
    }


@app.get("/api/bench/latest")
async def api_bench_latest() -> dict[str, Any]:
    """The published north-star bench card, for the Stats surface.

    The card was previously unreachable from the web app entirely — the UI's
    north-star row reads /api/metrics, which carries TASK counters, not bench
    results. A reader therefore could not see the two things that decide whether
    a headline means anything: how much of the corpus went unmeasured, and
    whether the run was even publishable.

    `refusals` is COMPUTED from the stored card on every read rather than
    persisted, so it can never go stale against the rules that produce it. It is
    evaluated with no baseline, so it reports the run's INTRINSIC problems
    (saturation, coverage, too few specs) and not "narrower than the previous
    run" — that comparison needs a baseline this endpoint does not have.

    404 ONLY when nothing has been recorded: the UI must say "no bench run yet"
    rather than render an all-zero card that looks like a catastrophic result.
    A card that exists but cannot be read is NOT a 404 — that would report a
    broken instrument as an idle one. It answers 200 with a zeroed card whose
    `refusals` say so, which is the surface the UI already renders as an alarm.
    """
    from ..eval.northstar_card import (
        RESULTS_DIR, NorthStarCard, published_file, publish_refusals,
    )

    path = RESULTS_DIR / "latest.json"
    # A hand-deleted latest.json must not hide an intact published baseline —
    # 404 means "nothing was ever recorded", and a clean publish WAS recorded.
    if not path.exists() and not published_file().exists():
        raise HTTPException(404, "no published bench run")
    # A card that EXISTS but cannot be read is a louder problem than no card at
    # all, and this is the one project where "the instrument is broken" must
    # never render as "nothing to see". `NorthStarCard.load` swallows OSError
    # and JSONDecodeError alike and returns None, and its per-score access uses
    # hard subscripts, so a truncated file, a chmod-000 file, a schema drift or
    # a non-object all ended as either a 404 saying "no bench run yet" (a lie)
    # or a 500 that blanked the panel. Report it through the surface the UI
    # already renders loudly: the refusals list.
    # BOTH failure shapes are logged, because `load` reports them differently:
    # it SWALLOWS OSError/JSONDecodeError and returns None, while schema drift
    # raises straight through. Either way the endpoint answers the same zeroed
    # card, so without a log there is nothing anywhere to diagnose from — and a
    # genuine programming error inside `load` (a new required BenchScore field
    # with no default) would render forever as "the recorded run could not be
    # read", the one case where this endpoint's own diagnosis is wrong.
    card = None
    if path.exists():
        try:
            card = NorthStarCard.load(path)
            if card is None:
                log.error("bench card at %s could not be read (unparseable or "
                          "unreadable)", path)
        except Exception:  # noqa: BLE001 — any schema failure, same verdict
            log.exception("bench card at %s could not be read", path)
            card = None
    # SCRUM-25: `latest.json` is the last PUBLISH CALL, clean or forced;
    # `published_baseline.json` (see `published_file()`) is the last CLEAN
    # one. A repo with no clean publish yet (or an older results dir
    # predating this file) has none — `published`/`latest_run` are then
    # simply absent and the response is exactly what this endpoint always
    # returned.
    baseline_card = None
    pub_path = published_file()
    if pub_path.exists():
        try:
            baseline_card = NorthStarCard.load(pub_path)
            if baseline_card is None:
                # load() swallows OSError/JSONDecodeError — log it here or a
                # truncated baseline is undiagnosable (same rule as latest.json).
                log.error("published baseline at %s could not be read "
                          "(unparseable or unreadable)", pub_path)
        except Exception:  # noqa: BLE001 — same "unreadable, not absent" rule
            log.exception("published baseline at %s could not be read",
                          pub_path)

    if baseline_card is not None:
        payload = _bench_payload(baseline_card, publish_refusals(baseline_card))
        payload["published"] = True
        # A footnote only when latest.json holds a DIFFERENT (necessarily
        # newer — nothing but `bench publish` writes either file, and a clean
        # publish writes both at once) run than the baseline itself.
        if card is not None and card.created_at != baseline_card.created_at:
            payload["latest_run"] = _bench_payload(card, publish_refusals(card))
        return payload

    if card is None:
        # Shaped from an EMPTY card rather than a hand-written key list, so the
        # error payload cannot drift out of the healthy payload's shape when a
        # field is added to the aggregate. A reader hitting a missing key here
        # would see the panel break in exactly the situation it exists to
        # explain.
        return _bench_payload(
            NorthStarCard(label="(unreadable)"),
            [f"the recorded run at {path.name} could not be read — it exists "
             f"but is unreadable or malformed, so no figure below can be "
             f"trusted"
             if path.exists() else
             f"{path.name} is missing and the published baseline could not "
             f"be read, so no figure below can be trusted"])
    return _bench_payload(card, publish_refusals(card))


@app.put("/api/projects/{project_id}")
async def api_update_project(
    project_id: str, body: UpdateProjectRequest, request: Request
) -> ProjectOut:
    store = _store(request)
    proj = await store.get_project(project_id)
    if not proj:
        raise HTTPException(404, "project not found")
    if body.name is not None:
        proj.name = body.name
    if body.repo_paths is not None:
        for rp in body.repo_paths:
            p = Path(rp).expanduser().resolve()
            if not p.is_dir() or not (p / ".git").exists():
                raise HTTPException(422, f"{rp!r} is not a git repository")
        proj.repo_paths = [str(Path(rp).expanduser().resolve()) for rp in body.repo_paths]
    if body.primary_repo is not None:
        proj.primary_repo = str(Path(body.primary_repo).expanduser().resolve())
    elif body.repo_paths is not None and proj.repo_paths:
        proj.primary_repo = proj.repo_paths[0]
    if body.test_layers is not None:
        from ..testing.test_layers import TestLayer
        validated = []
        for ld in body.test_layers:
            try:
                layer = TestLayer.from_dict(ld)
                validated.append(layer.to_dict())
            except Exception as exc:
                raise HTTPException(
                    422, f"invalid test layer {ld.get('name', '?')!r}: {exc}"
                )
        proj.test_layers = json.dumps(validated)
    await store.update_project(proj)
    return ProjectOut.from_project(proj)


@app.delete("/api/projects/{project_id}")
async def api_delete_project(project_id: str, request: Request) -> dict:
    store = _store(request)
    ok = await store.delete_project(project_id)
    if not ok:
        raise HTTPException(404, "project not found")
    return {"ok": True}


def _summarize_tool(tool: str, inp: dict) -> str:
    """Human-readable one-liner for an agent tool call."""
    if tool in ("Read", "View"):
        path = inp.get("file_path") or inp.get("path") or ""
        # Show just filename + parent dir to save space.
        parts = path.rsplit("/", 2)
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return f"Read {short}"
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("path") or ""
        parts = path.rsplit("/", 2)
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return f"Edit {short}"
    if tool in ("Grep", "Search"):
        q = inp.get("query") or inp.get("pattern") or ""
        path = inp.get("path") or inp.get("search_path") or ""
        parts = path.rsplit("/", 2)
        short = "/".join(parts[-2:]) if len(parts) >= 2 else path
        return f'Grep "{q[:60]}" in {short}'
    if tool in ("Glob", "ListDir"):
        pat = inp.get("pattern") or inp.get("path") or ""
        return f"Glob {pat[:80]}"
    if tool in ("Bash", "Terminal"):
        cmd = inp.get("command") or inp.get("cmd") or ""
        return f"Run `{cmd[:120]}`"
    # Fallback: tool name + first key.
    first = next(iter(inp.values()), "") if inp else ""
    return f"{tool} {str(first)[:80]}"


_RESULT_PREVIEW_CAP = 400  # chars of tool output surfaced in the activity feed

# The review verdict's substance. `text` says it in prose for a human, but the
# board decides PASS vs FAIL from `passed` and counts the findings from these —
# strip them and a PASSING round renders as "FAIL (? blocking)", which is worse
# than the silence this whole fix replaced. Same reason `message` is carried
# for the supervisor: on these events the meta IS the content.
_VERDICT_META = ("passed", "failed_count", "blocking_count", "advisory_count")


def _format_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending_tool_use: dict[str, Any] | None = None  # last tool_use awaiting its result
    for e in events:
        source = e.get("source", "")
        kind = e.get("kind", "")

        # Always include narration — see `is_narration`. This used to be a
        # hand-kept list of narration SOURCES and it drifted twice by omission:
        # `watcher` (the post-PR ladder starved out of the UI — 2015 events
        # served, 0 from watcher, found by the 2026-07-11 persona walk) and then
        # `reviewer` (the review verdict itself invisible). Both copies of the
        # list now ask the one predicate instead.
        if is_narration(source, kind) or kind in ("result", "error"):
            entry = {"ts": e.get("ts"), "kind": kind,
                     "text": e.get("text", ""), "source": source}
            # Carry the per-role model map so the System view can label each
            # node with the model that actually ran it.
            if kind == "models" and isinstance(e.get("models"), dict):
                entry["models"] = e["models"]
            # Constraint §6d part 2: a non-default reviewer backend is
            # disclosed as its own `role_backends` kwarg (never appended to
            # the clipped `text`) — whitelist it through like `models` above,
            # or `web/src/summaries.js`'s `nonDefaultReviewer` never sees it.
            if kind == "models" and isinstance(e.get("role_backends"), dict):
                entry["role_backends"] = e["role_backends"]
            # The substance of a supervisor decision lives in `message`, not
            # `text` — dropping it made the Supervisor look like it only ever
            # said "continue"/"correct" while its corrections carried real,
            # actionable guidance.
            if isinstance(e.get("message"), str) and e["message"]:
                entry["message"] = e["message"]
            for key in _VERDICT_META:
                if key in e:
                    entry[key] = e[key]
            out.append(entry)
            pending_tool_use = None
            continue

        # Agent tool_use: surface what the agent is doing (file, query, cmd),
        # plus the raw tool name/input so the UI can render file chips, and a
        # placeholder the next tool_result fills in below.
        if is_agent_session(source) and kind == "tool_use":
            tool = e.get("tool_name", "")
            inp = e.get("tool_input") or {}
            detail = _summarize_tool(tool, inp)
            entry = {"ts": e.get("ts"), "kind": "tool_use", "text": detail,
                      "source": source, "tool_name": tool, "tool_input": inp}
            out.append(entry)
            pending_tool_use = entry
            continue

        # Agent tool_result: attach a short preview to the tool_use it answers,
        # instead of silently discarding it — this is the actual output of the
        # call (file contents, grep matches, command stdout, etc.), not just
        # the call itself.
        if is_agent_session(source) and kind == "tool_result":
            text = (e.get("text") or "").strip()
            if text and pending_tool_use is not None:
                if len(text) > _RESULT_PREVIEW_CAP:
                    text = text[:_RESULT_PREVIEW_CAP] + "…"
                pending_tool_use["result_preview"] = text
            pending_tool_use = None
            continue

        # Agent prose (non-empty text blocks): show agent reasoning. Rendered
        # as markdown client-side, so keep a generous cap rather than the old
        # 600-char one-liner truncation.
        if is_agent_session(source) and kind == "text" and (e.get("text") or "").strip():
            text = (e.get("text") or "").strip()
            if len(text) > 4000:
                text = text[:3997] + "..."
            out.append({"ts": e.get("ts"), "kind": "agent_text",
                        "text": text, "source": source})
            pending_tool_use = None
            continue

        # Extended-thinking blocks ("Thought for Ns" in the UI) — previously
        # dropped entirely. Surface them as a distinct, collapsible-by-default
        # kind rather than silently discarding the model's reasoning.
        if is_agent_session(source) and kind == "thinking" and (e.get("text") or "").strip():
            text = (e.get("text") or "").strip()
            if len(text) > 4000:
                text = text[:3997] + "..."
            out.append({"ts": e.get("ts"), "kind": "thinking",
                        "text": text, "source": source})
            pending_tool_use = None
            continue

        # Subagent lifecycle (SDK Agent-tool TaskStarted/Progress/Notification).
        # The System view's agent tree discovers dynamically-spawned subagents
        # from these events (task_id/task_type/status). Dropping them here — as
        # this formatter previously did — meant subagents never appeared on
        # initial load or for finished tasks; only the live SSE stream surfaced
        # them. Mirror the SSE handling so both paths are consistent.
        if is_agent_session(source) and kind.startswith("subagent_"):
            entry = {"ts": e.get("ts"), "kind": kind,
                     "text": (e.get("text") or "").strip()[:300],
                     "source": source}
            for key in ("task_id", "task_type", "status"):
                if key in e:
                    entry[key] = e[key]
            out.append(entry)
            pending_tool_use = None
            continue

    return out


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request) -> list[dict[str, Any]]:
    """Return the complete event log for a task.

    The scheduler's in-memory buffer is a deque(maxlen=_MAX_EVENTS), so serving
    it alone silently truncated long runs: at 321 events the board received the
    last 158 and the Planner — whose events had aged out — vanished from the
    System view mid-run. The persisted copy is complete, so it is the base; the
    buffer only supplies the tail newer than the last flush (a couple of
    seconds' worth) and covers a task the store hasn't seen.
    """
    sched = getattr(request.app.state, "scheduler", None)
    buffered: list[dict[str, Any]] = []
    if sched is not None:
        for tid in list(sched._event_log.keys()):
            if tid.startswith(task_id):
                buffered = sched.task_events(tid)
                break

    store = _store(request)
    task = await store.find_task(task_id)
    persisted = await store.list_events(task.id) if task is not None else []
    if not persisted:
        return _format_events(buffered)

    last_ts = persisted[-1].get("ts") or 0
    tail = [e for e in buffered if (e.get("ts") or 0) > last_ts]
    return _format_events(persisted + tail)


# --------------------------------------------------------------------------- #
# Phase 4a: SSE streaming endpoint for live task events                        #
# --------------------------------------------------------------------------- #

def _resolve_task_id(sched, prefix: str) -> str | None:
    """Resolve a short task-id prefix to the full id in the event log."""
    for tid in list(sched._event_log.keys()):
        if tid.startswith(prefix):
            return tid
    return None


@app.get("/api/tasks/{task_id}/events/stream")
async def task_events_stream(task_id: str, request: Request):
    """SSE endpoint — streams task events as they arrive.

    The client opens an EventSource to this URL. Each SSE frame is a JSON
    object with {ts, kind, text, source}. The stream closes when the task
    leaves the inflight set and no more events arrive for 5 s.
    """
    sched = getattr(request.app.state, "scheduler", None)
    if sched is None:
        return PlainTextResponse("no scheduler", status_code=503)

    full_id = _resolve_task_id(sched, task_id)

    # W2.3: each frame carries `id: <ts>`, so the browser's NATIVE EventSource
    # reconnect resumes from where it dropped (Last-Event-ID header) instead
    # of replaying the whole deque or — worse — the client giving up. The
    # client no longer closes on transient errors.
    last_event_id = request.headers.get("last-event-id", "")

    async def _generate():
        nonlocal full_id
        try:
            last_ts = float(last_event_id)  # resume-from cursor on reconnect
        except (TypeError, ValueError):
            last_ts = 0.0  # timestamp-based cursor (deque rotates at maxlen=200)
        idle_ticks = 0
        while True:
            # Resolve lazily — task may start after SSE connection opens.
            if full_id is None:
                full_id = _resolve_task_id(sched, task_id)
                if full_id is None:
                    await asyncio.sleep(1)
                    idle_ticks += 1
                    if idle_ticks > 30:  # give up after 30 s
                        yield "data: {\"kind\": \"done\", \"text\": \"task not found\"}\n\n"
                        return
                    continue
                idle_ticks = 0  # resolved — reset so done-detection starts fresh

            events = sched.task_events(full_id)
            new_events = [e for e in events if (e.get("ts") or 0) > last_ts]
            for e in new_events:
                source = e.get("source", "")
                kind = e.get("kind", "")
                text = ""
                # Narration passes through live exactly as it does in
                # _format_events — the same predicate, so the replayed log and
                # the live stream can never disagree about what a human sees.
                if is_narration(source, kind) or kind in ("result", "error"):
                    text = e.get("text", "")
                elif is_agent_session(source) and kind == "tool_use":
                    text = _summarize_tool(e.get("tool_name", ""), e.get("tool_input") or {})
                    kind = "tool_use"
                elif is_agent_session(source) and kind == "text" and (e.get("text") or "").strip():
                    text = (e.get("text") or "").strip()[:600]
                    kind = "agent_text"
                elif is_agent_session(source) and kind.startswith("subagent_"):
                    text = (e.get("text") or "").strip()[:300]
                else:
                    continue
                frame_data = {"ts": e.get("ts"), "kind": kind,
                              "text": text, "source": source}
                if kind == "models" and isinstance(e.get("models"), dict):
                    frame_data["models"] = e["models"]
                # Mirror the _format_events whitelist so a live-streamed
                # reviewer disclosure matches the replayed log (§6d part 2).
                if kind == "models" and isinstance(e.get("role_backends"), dict):
                    frame_data["role_backends"] = e["role_backends"]
                if isinstance(e.get("message"), str) and e["message"]:
                    frame_data["message"] = e["message"]
                for key in _VERDICT_META:
                    if key in e:
                        frame_data[key] = e[key]
                if kind.startswith("subagent_"):
                    for key in ("task_id", "task_type", "status"):
                        if key in e:
                            frame_data[key] = e[key]
                frame = json.dumps(frame_data)
                yield f"id: {e.get('ts') or 0}\ndata: {frame}\n\n"
            if new_events:
                last_ts = max(e.get("ts") or 0 for e in new_events)

            # Check if task is done (not inflight and no new events for 5 ticks).
            if full_id not in sched.inflight:
                idle_ticks += 1
                if idle_ticks > 5:
                    yield "data: {\"kind\": \"done\", \"text\": \"stream ended\"}\n\n"
                    return
            else:
                idle_ticks = 0

            # Wait for new events or timeout.
            notify = sched._event_notify.get(full_id)
            if notify is not None:
                notify.clear()
                try:
                    await asyncio.wait_for(notify.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(1)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Keyed by the HEAD the note was computed against, not wall clock. A time-keyed
# cache can answer "not stale" after HEAD has moved, which is precisely the
# restart-races-a-landing case this flag exists to detect.
_stale_cache: tuple[str | None, str | None] | None = None
_stale_inflight = threading.Lock()


def _loaded_code_stale() -> str | None:
    """The advisory staleness note, keyed by the live checkout HEAD.

    A status read that wins the single-flight lock measures the checkout HEAD;
    a concurrent loser serves the last known answer without measuring, and so
    does a read whose HEAD lookup fails. The expensive ancestry check is
    cached only while that measured HEAD is unchanged. Purely informational:
    no caller gates on it, and by design nothing here can stop a task being
    claimed.

    Single-flight, and NON-BLOCKING about it. A cache miss alone is not enough
    to serialize on: the miss window is however long `staleness_note` takes,
    and the case this feature exists to detect is exactly the one where git is
    slow. Measured, rather than assumed: the board polls every 10s
    (`App.jsx`: `setInterval(poll, 10000)`), and with `loaded_code()` already
    warmed this path makes 1 git call when current and 2 when stale
    (`rev-parse`, then `merge-base`) — a 10-20s ceiling under `_GIT_TIMEOUT`,
    NOT `_detect`'s 30s, which is a different path. So a slow measurement
    outlives roughly two polls per open tab, and every one of those would start
    its own git: a process herd in precisely the degraded condition being
    measured, growing with the number of tabs.

    A loser therefore returns the LAST KNOWN answer instead of waiting. Waiting
    would trade a git herd for a thread-pool herd: these run under
    `asyncio.to_thread`, whose executor holds `min(32, cpu_count + 4)` threads
    — 16 here — so a dozen-odd waiters parked for the full ceiling would leave
    the whole process about one worker. Serving a slightly stale advisory value
    costs nothing: it is only retained while its observed HEAD is still live.

    With no cached value at all, a loser returns None, which renders as "no
    banner" — indistinguishable from "current". That is deliberate: during the
    first cold miss under concurrency the honest options are silence or a claim
    we have not finished checking, and silence errs toward not fabricating a
    staleness warning. It self-heals on the next poll.
    """
    global _stale_cache
    cached = _stale_cache          # read once; another thread may swap it
    if not _stale_inflight.acquire(blocking=False):
        return cached[1] if cached is not None else None
    try:
        from ..core.build_info import head_sha, loaded_code, staleness_note
        head = head_sha()
        if not head:
            # A HEAD we could not measure is not evidence the code is current.
            # `head_sha` fails soft for a missing git binary, a non-repository,
            # or a timeout (see `build_info._git`), and this path runs on EVERY
            # status read — the board polls every 10s (`App.jsx`) — so one
            # transient hiccup used to overwrite an established "behind HEAD"
            # verdict with (None, None), which renders bit-for-bit identically
            # to "your code is current". That is the one thing `build_info`
            # exists not to do: fail soft to unknown, never lie. Retain the last
            # answer and leave the cache untouched so the next successful
            # measurement re-keys it normally. With nothing cached this still
            # returns None — the deliberate first-cold-miss silence.
            return cached[1] if cached is not None else None
        if cached is not None and cached[0] == head:
            return cached[1]
        note = staleness_note(loaded_code(), head=head)
        _stale_cache = (head, note)
        return note
    finally:
        _stale_inflight.release()


def _stale_note_cached() -> str | None:
    """The only thing the `/api/worker/status` request path may call for this
    field: no subprocess, no thread hop, no lock acquisition. `_loaded_code_
    stale` used to run on every request that won its single-flight lock — a
    `rev-parse` (and a `merge-base` too, once behind) inline on the event
    loop's executor, on the order of 5-14s under load (2026-09-03 samples:
    5.5s, 13.9s). A background refresher (`_refresh_stale_note`) is now the
    only caller of `_loaded_code_stale`; this just reads its last answer."""
    cached = _stale_cache          # read once; a refresh may swap it
    return cached[1] if cached is not None else None


_STALE_REFRESH_S = 30.0


async def _refresh_stale_note() -> None:
    """Background twin of the old per-request measurement.

    Runs every `_STALE_REFRESH_S`, off the request path entirely, so
    `loaded_code_stale` is at most that far behind rather than exactly as
    fresh — and exactly as expensive — as the last poll. Sleeps FIRST:
    `lifespan` already seeds `_stale_cache` once at startup, so refreshing
    immediately here would be a redundant git call at boot.
    """
    while True:
        await asyncio.sleep(_STALE_REFRESH_S)
        try:
            await asyncio.to_thread(_loaded_code_stale)
        except Exception:  # noqa: BLE001 — advisory telemetry must never die
            log.debug("staleness refresh failed", exc_info=True)


@app.get("/api/worker/status")
async def worker_status(request: Request) -> dict[str, Any]:
    """Is the embedded worker running, how many tasks in-flight — and if none,
    WHY none?

    The first two fields alone cannot tell idle from wedged. On 2026-08-01 this
    endpoint returned `{"running":true,"inflight":0,"max_workers":4,
    "watcher_error":null}` for six hours while the scheduler's database view was
    frozen three hours in the past, re-dispatching two finished tasks ~12x/min
    and unable to see the one real task waiting. Every field was accurate.

    So `inflight` keeps its meaning and `idle_reason` supplies the one bit it
    never carried: `queue_empty` (nothing to do) vs `db_view_stale` (the queue
    only LOOKS empty) vs `quota_cooldown` vs `claimable_not_dispatched`. The
    counters beside it — crash rate, consecutive status-write failures, last
    successful write, stale detections and reconnects — are what makes the
    difference checkable from a single `curl` rather than by reading 46,000
    lines of log.

    ONE HONEST LIMIT, because it was measured rather than assumed. The
    staleness probe runs per scheduler TICK, not per request — a status
    endpoint that opened a second SQLite connection on every poll would put the
    board's polling on the database's critical path. So a poll landing between
    the wedge and the next tick still answers `healthy: true`; the flag is at
    most one `poll_interval` behind. `seconds_since_last_tick` is published for
    exactly that reason, and it doubles as the alarm for the failure no
    per-tick check can report on itself: a scheduler loop that has stopped
    ticking at all.

    `loaded_code` / `loaded_code_stale` answer a different question on the same
    poll: WHICH code is running. The server never reloads, so a merged fix is
    not live until it restarts. This flag is refreshed by a background task
    (`_refresh_stale_note`) at most `_STALE_REFRESH_S` behind, never by this
    request: measuring it inline used to mean an occasional request paid for
    a `git rev-parse` (and a `merge-base` too, once behind) synchronously —
    5-14s under load on 2026-09-03, on a process with four workers busy — for
    an advisory value nobody gates on. A read before the first background
    refresh serves `None`, the same first-cold-miss silence a lock loser
    already served.
    """
    sched = getattr(request.app.state, "scheduler", None)
    watcher_error = getattr(request.app.state, "watcher_error", None)
    # Set by the worker task's done-callback. `running: true` means "a Scheduler
    # object is wired up", which is NOT the same as "the loop is alive" — the
    # loop can die and leave the object answering.
    worker_error = getattr(request.app.state, "worker_error", None)
    common = {
        "watcher_error": watcher_error,
        "worker_error": worker_error,
        "loaded_code": getattr(request.app.state, "loaded_code", None),
        "loaded_code_stale": _stale_note_cached(),
    }
    if sched is None:
        return {"running": False, "inflight": 0, "max_workers": 0, **common,
                "idle_reason": "no_scheduler",
                "db_view_stale": False, "healthy": False}
    out: dict[str, Any] = {
        "running": True,
        "inflight": len(sched.inflight),
        "max_workers": sched.max_workers,
        **common,
    }
    snapshot = getattr(sched, "health_snapshot", None)
    if callable(snapshot):
        try:
            out.update(snapshot())
        except Exception as exc:  # noqa: BLE001 — status must always answer
            out["health_error"] = f"{type(exc).__name__}: {exc}"
    else:
        # FAIL CLOSED. Defaulting `healthy` to true for an object that cannot
        # describe itself is the same fail-open this change removed everywhere
        # else. Unreachable in production (one assignment site, always a real
        # Scheduler), which is exactly why it must not be left to luck.
        out["health_error"] = ("the scheduler cannot report its health "
                               "(no health_snapshot)")
    # One boolean for the surfaces that only want a light. Every clause is a
    # state that used to render as green, and the last two were added after a
    # review found the first version still had two reachable modes resolving to
    # `healthy: true` / `queue_empty` — the exact pre-incident reading:
    #
    #   * `tick_stalled` — the scheduler loop has stopped ticking. Every other
    #     field here is WRITTEN by a tick, so a stalled loop freezes them all in
    #     their last-known-good state and this endpoint reports the past.
    #     Publishing `seconds_since_last_tick` without consuming it surfaced
    #     nothing; a field with no reader is not a signal.
    #   * `consecutive_probe_failures` — the staleness detector itself is
    #     failing. That means the view is UNKNOWN, and "unknown" must not
    #     resolve to "healthy", or the detector fails open into the very silence
    #     it was built to break.
    #   * `worker_error` — the loop is DEAD. Nothing else here can say so:
    #     every other field is written by a tick, and a loop that died before
    #     its first tick leaves them all at their initial values.
    #
    # `tick_stalled` now also covers a loop that has NEVER ticked and has had
    # longer than its own threshold to do so, which is the same fault one step
    # earlier and reported `healthy: true` permanently until a review found it.
    # The two are complementary and neither replaces the other: the callback
    # catches a loop that DIED, the threshold catches one that is alive but
    # wedged inside a call that never returns — where no callback ever fires.
    out["healthy"] = (
        not out.get("db_view_stale", False)
        and not out.get("tick_stalled", False)
        and not out.get("consecutive_probe_failures", 0)
        and not out.get("consecutive_status_write_failures", 0)
        and watcher_error is None
        and worker_error is None
        and "health_error" not in out
    )
    return out


@app.get("/api/queue/health")
async def queue_health_endpoint(request: Request) -> dict[str, Any]:
    """D2 #4: is the queue stuck, and when does it drain? Pure timestamps."""
    from ..core.health import queue_health
    store: Store = request.app.state.store
    sched = getattr(request.app.state, "scheduler", None)
    inflight = set(sched.inflight) if sched is not None else set()
    max_workers = sched.max_workers if sched is not None else 0
    # getattr, not `sched.quota_cooldown_until`: existing test doubles (e.g.
    # SimpleNamespace(inflight=..., max_workers=...)) predate this field and
    # would otherwise AttributeError on every /api/queue/health call.
    quota_cooldown_until = getattr(sched, "quota_cooldown_until", None) if sched is not None else None
    infra_cooldown_until = getattr(sched, "infra_cooldown_until", None) if sched is not None else None
    h = await queue_health(store, inflight_ids=inflight, max_workers=max_workers,
                            quota_cooldown_until=quota_cooldown_until,
                            infra_cooldown_until=infra_cooldown_until)
    return h.as_dict()


@app.get("/api/metrics")
async def metrics(request: Request) -> dict[str, Any]:
    """The north-star numbers (M4): PRs opened/merged, attempts and tokens
    per PR, burn per auth profile, gate outcomes, repro-gate verdict split.
    Read-only SQL over the record — nothing derived, nothing cached."""
    from ..core.metrics import compute_metrics, playbook_outcomes
    data = await compute_metrics(_store(request))
    # D2 #5: which playbooks actually pay (gate rate + burn).
    data["by_playbook"] = await playbook_outcomes(request.app.state.store)
    return data


@app.get("/api/metrics/window")
async def metrics_window(request: Request, hours: float = 24.0) -> dict[str, Any]:
    """Spend that OCCURRED in the trailing window — attempt-attributed, so
    closing/cancelling an old task (bumping only `updated_at`) adds zero."""
    from ..core.metrics import window_spend
    if not (0 < hours <= 168):
        raise HTTPException(status_code=400, detail="hours must be in (0, 168]")
    return await window_spend(_store(request), hours=hours)


@app.get("/api/autonomy")
async def autonomy_report(request: Request, days: int | None = None) -> dict[str, Any]:
    """Autonomy telemetry (megaplan P0): mid-flight-touchpoint rate vs.
    PR-reached rate. Read-only."""
    from ..core.autonomy import compute_autonomy_metrics
    rep = await compute_autonomy_metrics(_store(request), days=days)
    return rep.as_dict()


@app.post("/api/tasks/{task_id}/reply")
async def reply_task(
    task_id: str, body: ReplyRequest, request: Request
) -> dict[str, Any]:
    """Store a human answer to a parked task's question; reset to IMPLEMENTING.

    Does NOT auto-run the orchestrator — the human runs `nh watch <id>` to resume.
    Parity with `nh reply --no-run`.
    """
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
    from ..core import plan_gate
    from ..core.bounds import Bounds
    from ..core.budget_floor import check_budget_floor

    store = _store(request)
    task = await _require_task(store, task_id)
    if task.status not in _PARKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"task is {task.status.value!r}, not a parked state — no question to answer",
        )
    # The install's effective bounds, so a stamped cap is the one the gate
    # will enforce (see `actions._normalised`) — computed once and reused
    # below for both the pre-dispatch budget-floor warning and apply_action.
    _bounds = Bounds.from_config((getattr(request.app.state, "config", None) or {}).get("bounds"))
    budget_warning = await check_budget_floor(store, task, bounds=_bounds)
    prior_status = task.status
    prior_blocker = task.blocker if isinstance(task.blocker, dict) else None
    ctx = task.context or {}
    replies = ctx.get("human_replies") or []
    blocker = task.blocker or {}
    question = blocker.get("question") if isinstance(blocker, dict) else None

    # Picking an option is the only path that applies its action, and it runs
    # only here, on a human's click.
    answer, applied, terminal, approves_plan = body.answer, None, False, False
    if body.choose is not None:
        options = Blocker.from_dict(blocker).options if blocker else []
        if not 1 <= body.choose <= len(options):
            raise HTTPException(
                status_code=400, detail=f"choose must be between 1 and {len(options)}",
            )
        option = options[body.choose - 1]
        answer = option.label
        terminal = is_terminal_action(option.action)
        approves_plan = is_plan_approval_action(option.action)
        try:
            applied = apply_action(task, option.action, bounds=_bounds)
        except ActionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    record = answer_record(
        question=question, answer=answer,
        attempt_id=(task.blocker or {}).get("attempt_id") or "",
        source="operator:api",
    )
    record["applied"] = applied
    await store.append_context_list(task.id, "human_replies", record)
    # Terminal option (SCRUM-22): the human chose "stop — keep parked". Record
    # the answer and leave the parked status untouched; resuming here is what
    # silently inverted the stop.
    if terminal:
        # Review 2026-07-25: without this stamp the wake watcher's sweep
        # undoes the stop — max_park re-escalates within 48h and any
        # wake_condition on the blocker RESUMES the task. The stamp makes the
        # human's decision durable; _evaluate skips human-stopped tasks.
        blocker_data = dict(task.blocker or {})
        blocker_data["human_stopped"] = True
        task.blocker = blocker_data
        task.wake_check_at = None
        await store.update_task_columns(task)
        tasks = await _board_tasks(store, scheduler=_sched(request))
        await _mgr.broadcast({
            "type": "task_updated", "task_id": task.id,
            "status": task.status.value,
            "tasks": [t.model_dump() for t in tasks],
        })
        return {"ok": True, "status": task.status.value, "kept_parked": True}
    patch: dict[str, Any] = {"wake_check_at": None}
    # Continue from the [WIP-BLOCKED] checkpoint rather than from base.
    checkpoint = resume_checkpoint(blocker)
    # Stamp the HUMAN provenance UNCONDITIONALLY — see `WakeWatcher._resume`.
    # The zero-diff honesty gate credits work ahead of base only when a human
    # gated it, and this stamp must OVERRIDE any `by: "wake"` an earlier machine
    # resume left behind. Writing it only `if checkpoint` is what let the stale
    # value survive: RFC 7396 merges nested dicts, so a blocker carrying no
    # `resume_commit` left the previous actor's `by` describing this answer, and
    # the human's reply was failed as fabrication.
    patch["resume_from"] = resume_provenance(checkpoint, "human")
    # GAP 1 plan-approval gate: at the gate, only the approve OPTION approves —
    # free text is a correction, which resumes into PLANNING to be re-planned
    # rather than into IMPLEMENTING. "At the gate" is read off the blocker the
    # human is actually answering (`plan_gate.at_gate`), not off a context
    # flag: nothing cleared that flag, so a stale one hijacked a later,
    # unrelated answer back into planning. Off the gate this is inert and the
    # resume target is IMPLEMENTING exactly as before.
    resume_to = plan_gate.resume_status(task, approve=approves_plan)
    if plan_gate.at_gate(task):
        patch[plan_gate.CONTEXT_KEY] = plan_gate.reply_patch(
            task, approve=approves_plan, answer=answer or "")
    task.context = await store.merge_context(task.id, patch)
    task.wake_check_at = None
    await store.update_task_columns(task)
    # Re-entering the loop withdraws any pending board Pause, or the next
    # attempt would honour it on turn zero and park the task straight back
    # (same as the CLI twin: cli/commands.py `nh task retry` / `nh reply`).
    await store.clear_cancel_request(task.id)
    await store.set_status(
        task, resume_to, validate=False,
        event=human_event(
            "reply", prior_status=prior_status, prior_blocker=prior_blocker,
            actor="operator:api"),
    )
    tasks = await _board_tasks(store, scheduler=_sched(request))
    await _mgr.broadcast({
        "type": "task_updated",
        "task_id": task.id,
        "status": resume_to.value,
        "tasks": [t.model_dump() for t in tasks],
    })
    return {
        "ok": True,
        "message": f"Reply stored. Run `nh watch {task_id[:8]}` to resume.",
        "budget_warning": budget_warning.as_dict() if budget_warning else None,
    }


# --------------------------------------------------------------------------- #
# Knowledge management: rules, skills, learnings, config                      #
# --------------------------------------------------------------------------- #


class _MemoryBody(BaseModel):
    title: str
    content: str
    tags: list[str] = []
    project: str | None = None


async def _with_usage_counts(
    store: Store, items: list[Any],
) -> list[dict[str, Any]]:
    """Attach the memory-lifecycle-A outcome split (success/failure/
    cancelled/timeout counts) to each memory row, for the Rules/Skills/
    Learnings panels. `use_count`/`last_used_at` are already columns on
    `memories` and travel with `dict(r)`; only the split needs a join.
    Correlational, not causal — the web cards must label it so."""
    rows = [dict(r) for r in items]
    counts = await store.memory_outcome_counts([r["id"] for r in rows if r.get("id")])
    zero = {"success_count": 0, "failure_count": 0,
            "cancelled_count": 0, "timeout_count": 0}
    for r in rows:
        r.update(counts.get(r.get("id"), zero))
    return rows


@app.get("/api/rules")
async def list_rules(
    request: Request, include_archived: bool = False,
) -> list[dict[str, Any]]:
    store = _store(request)
    from ..learning import TYPE_RULE, TYPE_ANTI_PATTERN
    items = await store.list_memories(
        confirmed=True, mem_type=TYPE_RULE, include_archived=include_archived)
    items += await store.list_memories(
        confirmed=True, mem_type=TYPE_ANTI_PATTERN, include_archived=include_archived)
    return await _with_usage_counts(store, items)


@app.post("/api/rules", status_code=201)
async def add_rule(body: _MemoryBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    from ..learning import TYPE_RULE
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title=body.title, content=body.content,
        tags=body.tags, project=body.project,
        source="board", confirmed=True,
    )
    if not mem_id:
        raise HTTPException(status_code=409, detail="Duplicate rule")
    return {"ok": True, "id": mem_id, "title": body.title}


@app.delete("/api/rules/{rule_id}")
async def remove_rule(rule_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    m = await store.find_memory(rule_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"rule {rule_id!r} not found")
    await store.delete_memory(m["id"])
    return {"ok": True, "id": m["id"]}


@app.get("/api/skills")
async def list_skills(
    request: Request, include_archived: bool = False,
) -> list[dict[str, Any]]:
    store = _store(request)
    from ..learning import TYPE_SKILL, TYPE_FACT
    items = await store.list_memories(
        confirmed=True, mem_type=TYPE_SKILL, include_archived=include_archived)
    items += await store.list_memories(
        confirmed=True, mem_type=TYPE_FACT, include_archived=include_archived)
    return await _with_usage_counts(store, items)


@app.post("/api/skills", status_code=201)
async def add_skill(body: _MemoryBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    from ..learning import TYPE_SKILL
    mem_id = await store.add_memory(
        mem_type=TYPE_SKILL, title=body.title, content=body.content,
        tags=body.tags, project=body.project,
        source="board", confirmed=True,
    )
    if not mem_id:
        raise HTTPException(status_code=409, detail="Duplicate skill")
    return {"ok": True, "id": mem_id, "title": body.title}


@app.delete("/api/skills/{skill_id}")
async def remove_skill(skill_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    m = await store.find_memory(skill_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"skill {skill_id!r} not found")
    await store.delete_memory(m["id"])
    return {"ok": True, "id": m["id"]}


@app.get("/api/learnings")
async def list_learnings(
    request: Request, active: bool = False, include_paused: bool = False,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """``include_paused``/``include_archived`` (D3.2, 2026-09-01 review
    deferrals) only affect the ``active=true`` branch — `pending()` takes
    neither kwarg (it is a plain `list_memories(confirmed=False,
    source=SOURCE_PROPOSED)` call with no paused/archived handling of its
    own), so the query string is simply ignored on that branch rather than
    422ing a caller that always sends both regardless of which view it's
    requesting. (Both flags CAN apply to a pending row in principle —
    `pause()`/`delete()` both work "on a row of any status" per their own
    docstrings — but a pending row that is also paused or archived already
    falls out of `pending()`'s result today, independent of this change; the
    Second-brain UI never calls delete/pause against an unconfirmed row in
    the first place, so this is not a path either flag needs to reach.) The
    Second-brain UI passes ``include_paused=true`` so a paused row stays
    visible (with its own `Paused` chip) instead of vanishing the moment
    Pause is clicked, and ``include_archived=true`` so a just-deleted
    (archived) row is likewise recoverable from the same list's archived-
    count footer rather than only "recoverable" in the sense that the byte-
    for-byte row still exists somewhere no UI shows it — the same rows
    `Store.list_memories`'s defaults would otherwise hide from every caller,
    injection included."""
    store = _store(request)
    from ..learning import LearningQueue
    q = LearningQueue(store)
    rows = await (
        q.active(include_paused=include_paused, include_archived=include_archived)
        if active else q.pending())
    return await _with_usage_counts(store, rows)


# Registered BEFORE any `/api/learnings/{mem_id}` route (constraint noted in
# PLAN.md — belt-and-suspenders even though the method/suffix already
# disambiguate it from the POST .../{mem_id}/retire below) so a literal path
# segment is never captured by a path parameter.
@app.get("/api/learnings/retire-candidates")
async def learnings_retire_candidates(
    request: Request, days: int = 90,
) -> list[dict[str, Any]]:
    """Memory lifecycle C, AC2: stale ACTIVE (confirmed) rules — SUGGEST
    only. Read-only; nothing here archives anything."""
    store = _store(request)
    from ..learning.retire import retirement_candidates
    rows = await retirement_candidates(store, days=days)
    return [dict(r) for r in rows]


@app.post("/api/learnings/{mem_id}/confirm")
async def confirm_learning(mem_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"proposal {mem_id!r} not found")
    from ..learning import LearningQueue
    await LearningQueue(store).confirm(m["id"])
    return {"ok": True, "id": m["id"]}


@app.post("/api/learnings/{mem_id}/reject")
async def reject_learning(mem_id: str, request: Request) -> dict[str, Any]:
    """Kept for CLI/API compat (D3, 2026-08-31 operator directive). On a
    still-PENDING proposal this is unchanged — the per-origin archive/delete
    dispatch `LearningQueue.reject` has always done. On an already-CONFIRMED
    learning (the common case now that most proposals auto-activate) it
    ALIASES `pause` instead: see `LearningQueue.reject`'s docstring for why
    deleting/archiving an active rule by the old per-origin table was never
    the right behaviour for one."""
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"proposal {mem_id!r} not found")
    from ..learning import LearningQueue
    await LearningQueue(store).reject(m["id"])
    return {"ok": True, "id": m["id"]}


@app.post("/api/learnings/{mem_id}/pause")
async def pause_learning(mem_id: str, request: Request) -> dict[str, Any]:
    """D3: the Second-brain UI's Pause action. The row stays (recoverable),
    ``paused=1``, never injected again. Works on any row regardless of
    confirmed status; idempotent (pausing an already-paused row is a no-op
    200, not an error)."""
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"learning {mem_id!r} not found")
    from ..learning import LearningQueue
    await LearningQueue(store).pause(m["id"])
    return {"ok": True, "id": m["id"]}


@app.post("/api/learnings/{mem_id}/delete")
async def delete_learning(mem_id: str, request: Request) -> dict[str, Any]:
    """D3: the Second-brain UI's Delete action. Archives the row — never a
    real ``DELETE FROM`` — mirroring `curator.py`'s never-deletes invariant;
    recoverable via ``POST /api/learnings/{id}/restore``."""
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"learning {mem_id!r} not found")
    from ..learning import LearningQueue
    await LearningQueue(store).delete(m["id"])
    return {"ok": True, "id": m["id"]}


@app.post("/api/learnings/{mem_id}/retire")
async def retire_learning(mem_id: str, request: Request) -> dict[str, Any]:
    """Memory lifecycle C, AC2: the human's explicit yes to a `retire?`
    suggestion. 404 unknown id; 409 if the row is not confirmed (retirement
    is for ACTIVE rules — an unconfirmed proposal has `reject` for that job).
    Idempotent: retiring an already-archived row returns
    ``{"ok": True, "already_archived": True}`` rather than an error, since a
    dismissed-then-retried client action should never surface as a failure."""
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"learning {mem_id!r} not found")
    if m.get("archived"):
        return {"ok": True, "id": m["id"], "already_archived": True}
    if not m.get("confirmed"):
        raise HTTPException(
            status_code=409,
            detail="only a confirmed (active) rule can be retired — "
                   "reject the pending proposal instead")
    from ..learning import LearningQueue
    await LearningQueue(store).retire(m["id"])
    return {"ok": True, "id": m["id"]}


@app.post("/api/learnings/{mem_id}/restore")
async def restore_learning(mem_id: str, request: Request) -> dict[str, Any]:
    """Memory lifecycle C part B: the Rules/Skills UI's triage action — a
    human's explicit undo of an archive, whatever produced it (the 45-day
    sweep, a supersede-on-confirm, a manual or auto-retire, or the D3 Delete
    action). 404 unknown id; idempotent on a row that is already live
    (``already_active: True``, 200 — the same double-click contract
    `retire_learning` chose, so a stale button never surfaces as a failure).

    D3: ALSO undoes a Pause — `archived` and `paused` are independent flags
    (a row can be paused without ever being archived), and this is the
    Second-brain UI's one Restore button for both, so a caller never has to
    know which inert state a row is in before clicking it. A row that is
    BOTH archived and paused (possible: retire, then pause it while it sits
    archived) is restored on both axes in one call.
    """
    store = _store(request)
    m = await store.find_memory(mem_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"learning {mem_id!r} not found")
    was_archived, was_paused = bool(m.get("archived")), bool(m.get("paused"))
    if not was_archived and not was_paused:
        return {"ok": True, "id": m["id"], "already_active": True}
    from ..learning import LearningQueue
    if was_archived:
        await store.unarchive_memory(m["id"])
    if was_paused:
        await LearningQueue(store).unpause(m["id"])
    return {"ok": True, "id": m["id"]}


@app.get("/api/memories/quarantine")
async def quarantine_counts(request: Request) -> dict[str, int]:
    """Per-panel quarantined row counts (P1 brain hygiene) — an honest
    footer, not a changed list shape. `/api/rules` and `/api/skills` keep
    returning a bare list; this is a NEW endpoint so their response shape
    stays untouched.

    ``total`` is deliberately the ALL-TYPES quarantined count, not
    ``rules + skills`` — the memories table has more types than the four the
    Rules/Skills panels cover (e.g. proposals), and a row of one of those can
    be quarantined without ever surfacing in either panel. So
    ``total >= rules + skills`` is expected, not a double-count bug (round-3
    review advisory 2): the Learnings footer is deliberately the grand total
    across every type, while ``rules``/``skills`` are the two panel subsets.
    """
    store = _store(request)
    from ..learning import TYPE_ANTI_PATTERN, TYPE_FACT, TYPE_RULE, TYPE_SKILL
    rules = (await store.count_quarantined(mem_type=TYPE_RULE)
             + await store.count_quarantined(mem_type=TYPE_ANTI_PATTERN))
    skills = (await store.count_quarantined(mem_type=TYPE_SKILL)
              + await store.count_quarantined(mem_type=TYPE_FACT))
    all_types_total = await store.count_quarantined()
    return {"rules": rules, "skills": skills, "learnings": all_types_total,
            "total": all_types_total}


_SECRET_KEY_RE = re.compile(r"(token|secret|password|webhook|key)", re.IGNORECASE)


def _scrub_secrets(value: Any) -> Any:
    """Recursively replace secret-shaped string values with a marker.

    Any dict key matching `_SECRET_KEY_RE` whose value is a non-empty string
    is replaced with "●●● set". Empty/None values pass through unchanged.
    Operates on (and returns) a fresh structure — callers must pass a
    deep copy so the running config is never mutated.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if (
                isinstance(k, str)
                and _SECRET_KEY_RE.search(k)
                and isinstance(v, str)
                and v
            ):
                out[k] = "●●● set"
            else:
                out[k] = _scrub_secrets(v)
        return out
    if isinstance(value, list):
        return [_scrub_secrets(item) for item in value]
    return value


@app.get("/api/config")
async def show_config(request: Request) -> dict[str, Any]:
    """Return the current config (safe subset — no secrets), plus two
    read-only, server-derived fields the board's task composer uses to
    build its coder-backend picker:

    * ``coder_backends`` — the tuple ``agent.backend.SUPPORTED_BACKENDS``
      accepts. A backend added to that tuple shows up here, and therefore in
      the composer, with no JS change — the composer must never hardcode its
      own copy of this list.
    * ``claude_pinned_roles`` — ``agent.backend.CLAUDE_PINNED_ROLES``, the
      roles that stay on Claude no matter which coder backend is chosen, so
      the composer can say so instead of asserting it as a second literal
      that could drift from the one `make_backend` actually enforces.
    * ``coder_backend_availability`` — one ``{"id", "available", "reason"}``
      per entry in ``coder_backends``, from
      ``core.backend_settings.describe_backend``: the SAME
      ``core.runtime.assert_task_backend_usable`` preflight the orchestrator
      runs before the first coder turn, never a frontend heuristic
      re-deriving "does 'local' have a base_url" or "is codex logged in" on
      its own — a duplicated rule here is exactly what could disagree with
      the CLI/API's own refusal. The task composer greys out an option (and
      shows this ``reason``) using this field instead.
    * ``coder_backend_effective`` — ``agent.backend.resolve_backend_name``
      applied to THIS config: what the coder will actually run on right now,
      whether that came from an explicit ``worker.backend`` or the function's
      own ``"claude"`` fallback. The composer must never re-derive this
      precedence itself (a second copy in JS could silently diverge from the
      one function `make_backend` actually calls).
    * ``coder_backend_default`` — the pristine default
      (``DEFAULT_CONFIG["worker"]["backend"]``) ``coder_backend_effective`` is
      compared against, so the composer can tell "this install configured
      something non-default" from "nothing was edited" without naming a
      backend of its own. Precedent: ``core/backend_settings.py`` already
      ships ``"default": "claude"`` on ``/api/coder-backend`` the same way.

    All five are computed fresh on every call (not config data), so they
    can never be "scrubbed" or otherwise altered by ``_scrub_secrets``.
    """
    cfg = request.app.state.config
    data = copy.deepcopy(cfg.data)
    scrubbed = _scrub_secrets(data)
    from ..agent.backend import CLAUDE_PINNED_ROLES, SUPPORTED_BACKENDS, resolve_backend_name
    from ..config import DEFAULT_CONFIG
    from ..core.backend_settings import describe_backend
    scrubbed["coder_backends"] = list(SUPPORTED_BACKENDS)
    scrubbed["claude_pinned_roles"] = list(CLAUDE_PINNED_ROLES)
    scrubbed["coder_backend_availability"] = [
        describe_backend(name, cfg.data) for name in SUPPORTED_BACKENDS
    ]
    scrubbed["coder_backend_effective"] = resolve_backend_name(cfg.data)
    scrubbed["coder_backend_default"] = DEFAULT_CONFIG["worker"]["backend"]
    # Live, not cached off app.state, so an added credential lifts the
    # banner on the next poll (see `_require_credentials`). Only reported
    # for apps that opted in the same way that function gates on.
    if hasattr(request.app.state, "setup_mode"):
        from ..config import subscription_credential_missing
        scrubbed["setup_mode"] = subscription_credential_missing(cfg.data) is not None
    else:
        scrubbed["setup_mode"] = False
    return scrubbed


@app.get("/api/version")
async def show_version() -> dict[str, Any]:
    """The running `nh` version, and whether the browser Updates panel may
    print a pip command for it.

    The board runs in two places. Inside the desktop shell the version arrives
    over the preload bridge as ``window.nhDesktop.version``; in a plain browser
    there is no bridge, so Settings > Updates printed "You are running no_human
    unknown in a browser". The server always knows — it IS the installed
    package — so it says so. Deliberately NOT folded into /api/config: a version
    is not configuration, and that payload is already broader than it should be.

    ``no_human.__version__`` is the same string ``nh --version`` prints and the
    same one the update check compares against, so all three agree by
    construction.

    ``dist_name``/``published`` let the browser panel derive its upgrade
    instruction from the real distribution channel instead of hardcoding a
    package name that may not exist there yet: ``published`` is fail-closed
    (``updates.is_published()`` never raises and defaults to False), so an
    absent or unreadable cache reads as "not provably published", never as a
    false "yes".
    """
    from .. import __version__, updates

    return {
        "version": __version__,
        "dist_name": updates.DIST_NAME,
        "published": updates.is_published(),
    }


@app.get("/api/integrations")
async def list_integrations_endpoint(request: Request) -> dict[str, Any]:
    """Status of every integration (configured + kind; healthy is null until a
    `test` is run), PLUS its `fields` array so the UI can render a settings
    form. Never returns a secret — `fields` carries only `set: bool`."""
    from ..integrations import integration_fields, list_integrations_with_health
    cfg = request.app.state.config
    out = []
    # The ambient overlay can shell out to `gh`/`git` (subprocess.run with a
    # multi-second timeout) — the same asyncio.to_thread discipline the rest
    # of this file uses for blocking work, so a slow/hanging CLI probe never
    # freezes the single-threaded event loop (SSE, task list, every request).
    statuses = await asyncio.to_thread(list_integrations_with_health, cfg.data)
    for s in statuses:
        d = asdict(s)
        d["fields"] = integration_fields(s.name, cfg.data)
        out.append(d)
    return {"integrations": out}


@app.get("/api/integrations/setup")
async def integration_setup_specs(request: Request) -> dict[str, Any]:
    """What the onboarding "Connect your tools" step renders itself from.

    One entry per block under ``DEFAULT_CONFIG["integrations"]`` — DISCOVERED,
    not a list of names in the UI, so adding a sixth block makes a sixth card
    appear with no frontend change. Carries the non-secret current values, the
    on/off switch, and the NAMES of the ~/.no_human/.env variables each
    integration's credential needs (plus whether each is set) — never a secret
    value, and never a field the wizard is allowed to write a secret into."""
    from ..integrations import setup_specs
    cfg = request.app.state.config
    repos = await _registered_repo_paths(request)
    return {"integrations": setup_specs(cfg.data, repos)}


@app.put("/api/integrations/{name}/setup")
async def save_integration_setup(
    name: str, body: IntegrationSetupRequest, request: Request
) -> dict[str, Any]:
    """Persist one integration's NON-SECRET onboarding settings to config.yaml.

    Distinct from ``/api/integrations/{name}/config`` on purpose: that route
    can route a field to ~/.no_human/.env, this one writes config.yaml ONLY
    and refuses (422) any field that reads as a credential, so the wizard can
    never put a token in a world-readable file. Same local-origin guard as
    every other config write."""
    from ..integrations import RepoNotRegistered, apply_setup

    require_local_origin(request, writing=True)
    repos = await _registered_repo_paths(request)
    try:
        spec = await asyncio.to_thread(apply_setup, name, dict(body.values), repos)
    except RepoNotRegistered as exc:
        # A bad VALUE (default_repo names no registered repo) — 400, not 422.
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        # Unknown integration/field and "that's a credential" are both the
        # caller's mistake; 422 carries the message the UI shows verbatim.
        raise HTTPException(status_code=422, detail=str(exc))

    from ..config import CONFIG_PATH, load_config
    refreshed = load_config(CONFIG_PATH)
    request.app.state.config.data = refreshed.data
    return spec


@app.post("/api/integrations/{name}/test")
async def test_integration_endpoint(name: str, request: Request) -> dict[str, Any]:
    """Run a live health check for one integration. The returned `detail` is a
    human-readable message that never contains a token or secret."""
    from ..config import AuthError, load_env_var
    from ..integrations import (
        FIELD_SPECS,
        test_integration as run_integration_test,
    )

    # This route is NOT a no-op read: it loads .env secrets, fires authenticated
    # OUTBOUND calls with the operator's stored tokens, and (on a pass) writes
    # config.yaml via mark_verified. The app is unauthenticated (the loopback
    # boundary is the only guard), so — exactly like /setup and /config — it MUST refuse a
    # cross-origin caller, or a page the operator merely visits could drive a
    # probe with their token and read back the VCS username/project in `detail`.
    require_local_origin(request, writing=True)

    # Load this integration's secret(s) from ~/.no_human/.env into the process
    # env BEFORE the health check. Without this the button could only
    # authenticate when the server happened to be started via `nh serve` (whose
    # Jira poll loads JIRA_API_TOKEN) — from a plain `nh start`, the token was
    # absent and every "Test connection" reported it unset. The .env stays the
    # only source of the secret; config comes from app.state like every other
    # endpoint (updated by the save endpoint / a restart), keeping test
    # isolation intact.
    cfg = request.app.state.config
    for spec in FIELD_SPECS.get(name, []):
        if spec.env_var:
            try:
                load_env_var(spec.env_var)
            except AuthError:
                # A metered-auth var is never an integration secret; skip it
                # rather than 500 the whole test.
                pass
    try:
        status = await run_integration_test(name, cfg.data)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # A PASSING test is the one thing that earns a green "Ready": persist
    # integrations.<name>.last_verified_at so the wizard shows Ready across
    # reloads, not just in this session. mark_verified writes nothing for a
    # ci.* view (github/gitlab/jenkins/circleci) — see its docstring — and never
    # touches a credential.
    if status.healthy is True:
        from ..integrations import mark_verified

        if await asyncio.to_thread(mark_verified, name):
            from ..config import CONFIG_PATH, load_config
            request.app.state.config.data = load_config(CONFIG_PATH).data
    return asdict(status)


@app.put("/api/integrations/{name}/config")
async def save_integration_config_endpoint(
    name: str, body: SaveIntegrationConfigRequest, request: Request
) -> dict[str, Any]:
    """Persist one integration's settings-form fields: secrets to
    ``~/.no_human/.env``, everything else to ``config.yaml``. Returns the
    refreshed status card PLUS its `fields` array — NEVER a secret value."""
    from ..config import AuthError
    from ..integrations import KIND_BY_NAME, integration_fields, save_integration_config

    # This route writes ~/.no_human/.env — the SAME credential store the auth
    # endpoint guards. Without this (before the loopback boundary), any page the
    # operator visited while `nh serve` was up could preflight successfully and then
    # PUT a planted secret into it; that drive-by was demonstrated end to end.
    require_local_origin(request, writing=True)
    if name not in KIND_BY_NAME:
        raise HTTPException(status_code=404, detail=f"unknown integration: {name!r}")
    try:
        # save_integration_config overlays an ambient probe that shells out
        # (subprocess.run) — offload it exactly like the list endpoint above,
        # or a settings save freezes the single-threaded loop for up to 2s.
        status = await asyncio.to_thread(save_integration_config, name, body.fields)
    except (ValueError, AuthError) as exc:
        # AuthError is what the shared writer's line guard raises. Uncaught it
        # became a 500 — and because the values are written one key at a time,
        # an earlier key had already landed before a later one was refused.
        raise HTTPException(status_code=422, detail=str(exc))

    # Reload so this response (and subsequent requests) see what was just
    # written — CONFIG_PATH is looked up fresh here too (see integrations'
    # write-path comment), never a stale bound default.
    from ..config import CONFIG_PATH, load_config

    refreshed = load_config(CONFIG_PATH)
    request.app.state.config.data = refreshed.data
    _record_feature_used(request, FEATURE_INTEGRATION_SAVED)
    out = asdict(status)
    out["fields"] = integration_fields(name, refreshed.data)
    return out


@app.put("/api/telemetry/consent")
async def save_telemetry_consent(
    body: TelemetryConsentRequest, request: Request
) -> dict[str, Any]:
    """Persist `telemetry.enabled` to config.yaml (the config-level opt-out).

    On FIRST enable, mints the anonymous `telemetry.instance_id` (uuid4)
    HERE, server-side, in the same write — the id never comes from the
    browser. ON by default; turning it off writes `enabled: false` and
    leaves the id in place (so re-enabling keeps one stable anonymous id
    rather than manufacturing a fresh "new install" every toggle)."""
    import uuid

    from ..integrations import _write_config_values

    require_local_origin(request, writing=True)
    from ..config import CONFIG_PATH, load_config

    updates: dict[str, Any] = {"telemetry.enabled": bool(body.enabled)}
    current = load_config(CONFIG_PATH).data.get("telemetry") or {}
    if body.enabled and not str(current.get("instance_id") or "").strip():
        updates["telemetry.instance_id"] = str(uuid.uuid4())
    await asyncio.to_thread(_write_config_values, CONFIG_PATH, updates)

    refreshed = load_config(CONFIG_PATH)
    request.app.state.config.data = refreshed.data
    # Recompute the CSP now (same builder the lifespan uses at app start), so
    # the widened/strict header tracks consent without waiting for a restart.
    request.app.state.csp = _build_csp(refreshed.data)
    tel = copy.deepcopy(refreshed.data.get("telemetry") or {})
    # Replay/init runs at page bootstrap, so the browser reloads to apply.
    return {"telemetry": _scrub_secrets(tel), "reload_required": True}


@app.get("/api/models")
async def api_get_models(request: Request) -> dict[str, Any]:
    """The model picker's catalog + current values, for Settings.

    Options and defaults come ONLY from ``model_catalog``; ``current`` comes
    from the RUNNING process's bound config (``app.state.config.data``) — the
    same "what does this server actually believe right now" source every
    other GET in this file reads. ``restart_required`` is a true file-vs-
    process comparison performed inside ``model_settings.models_payload``,
    the same shape of check ``/api/auth/status`` already does for the auth
    profile.
    """
    from ..config import CONFIG_PATH
    from ..core import model_settings

    require_local_origin(request)
    cfg = getattr(request.app.state, "config", None)
    data = getattr(cfg, "data", None) or {}
    return await asyncio.to_thread(model_settings.models_payload, data, CONFIG_PATH)


@app.put("/api/config/models")
async def api_set_config_models(request: Request) -> dict[str, Any]:
    """Change up to all five ``llm.*_model`` keys in one write.

    The body is parsed BY HAND (not pydantic) so a malformed request gets one
    short, operator-facing sentence — not pydantic's auto-generated error
    tree — that may quote the bad key/value back to the caller (origin-
    gated). Validation and the write both run through the exact function
    ``model_settings.apply_model_changes`` that ``nh config models set``
    calls, so the API and CLI can never drift.

    Deliberately does NOT reload ``request.app.state.config`` (a model change
    only takes effect next task) — reloading would make ``restart_required`` lie.

    Constraint amendment §6d: the body may ALSO carry a ``role_backends`` key
    (``{role: {"backend", "model"} | null}``, today ``"reviewer"`` only) —
    ``apply_model_changes`` delegates it to ``role_backend_settings`` and
    re-wraps any refusal as the same ``ModelSettingsError`` caught below.
    """
    from ..config import CONFIG_PATH, AuthError
    from ..core import model_settings

    require_local_origin(request, writing=True)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — never surface the raw body
        raise HTTPException(422, "expected a JSON object") from None

    cfg = getattr(request.app.state, "config", None)
    running_data = getattr(cfg, "data", None) or {}
    try:
        payload, changes = await asyncio.to_thread(
            model_settings.apply_model_changes,
            body,
            running_cfg_data=running_data,
            config_path=CONFIG_PATH,
        )
    except (model_settings.ModelSettingsError, AuthError) as exc:
        raise HTTPException(422, str(exc)) from exc

    if changes:
        store = _store(request)
        event = model_settings.model_change_event(changes)
        await store.save_events(model_settings.CONFIG_AUDIT_TASK_ID, [event])
        await _mgr.broadcast({
            "type": "task_event",
            "task_id": model_settings.CONFIG_AUDIT_TASK_ID,
            "event": event,
        })
    return payload


@app.get("/api/coder-backend")
async def api_get_coder_backend(request: Request) -> dict[str, Any]:
    """Settings' coder-backend row: the current GLOBAL default
    (``worker.backend``) plus, for every entry in
    ``agent.backend.SUPPORTED_BACKENDS``, whether THIS install can run it
    right now and why not if it can't.

    Availability comes from ``core.backend_settings.describe_backend``,
    which calls the exact same ``core.runtime.assert_task_backend_usable``
    preflight the orchestrator itself runs before the first coder turn — so
    a backend the board greys out here is the same one a per-task
    ``--backend`` override would fail on, never a second, divorceable
    opinion. ``restart_required`` mirrors ``/api/models``'s file-vs-process
    comparison (``worker.backend`` is read at the same construction site,
    ``core.runtime.build_orchestrator``, bound at server start).
    """
    from ..config import CONFIG_PATH
    from ..core import backend_settings

    require_local_origin(request)
    cfg = getattr(request.app.state, "config", None)
    data = getattr(cfg, "data", None) or {}
    return await asyncio.to_thread(backend_settings.backend_payload, data, CONFIG_PATH)


@app.put("/api/config/coder-backend")
async def api_set_coder_backend(request: Request) -> dict[str, Any]:
    """Change the GLOBAL default coder backend (``worker.backend``).

    A per-task ``--backend``/composer override (``task.config["backend"]``,
    read by ``core.runtime.task_backend_override``) is untouched by this —
    it only moves the default a task falls back to when it names none. The
    body is parsed by hand, same as ``/api/config/models``, so a malformed
    request gets one short operator-facing sentence. Validation and the
    write both run through ``core.backend_settings.apply_backend_change`` —
    the same function any future CLI twin would call, so the two can never
    enforce different rules.

    Deliberately does NOT reload ``request.app.state.config`` for the same
    reason ``/api/config/models`` does not: the orchestrator reads
    ``config.data`` per task, not per request, so a change here only takes
    effect on the NEXT task (until a restart), and reloading here would make
    ``restart_required`` lie the moment this handler returns.
    """
    from ..config import CONFIG_PATH, AuthError
    from ..core import backend_settings

    require_local_origin(request, writing=True)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — never surface the raw body
        raise HTTPException(422, "expected a JSON object") from None

    cfg = getattr(request.app.state, "config", None)
    running_data = getattr(cfg, "data", None) or {}
    try:
        payload, changes = await asyncio.to_thread(
            backend_settings.apply_backend_change,
            body,
            running_cfg_data=running_data,
            config_path=CONFIG_PATH,
        )
    except (backend_settings.BackendSettingsError, AuthError) as exc:
        raise HTTPException(422, str(exc)) from exc

    if changes:
        store = _store(request)
        event = backend_settings.backend_change_event(changes)
        await store.save_events(backend_settings.CONFIG_AUDIT_TASK_ID, [event])
        await _mgr.broadcast({
            "type": "task_event",
            "task_id": backend_settings.CONFIG_AUDIT_TASK_ID,
            "event": event,
        })
    return payload


def _workers_payload(file_data: dict, running_data: dict) -> dict[str, Any]:
    """Settings' worker-count row: the ON-DISK ``concurrency`` values, the
    EFFECTIVE pool this machine would actually run them at (clamped by
    ``resolve_max_workers`` — CPU + isolation aware), and whether a restart is
    needed for a written change to take hold.

    ``restart_required`` compares the file against the RUNNING process config
    (``resolve_max_workers`` is read once at server start, so a fresh write
    only takes effect on the next ``nh serve``) — the same file-vs-process
    signal ``/api/config/models`` and ``/api/coder-backend`` report.
    """
    from ..core.scheduler import resolve_max_workers
    from ..config import _MAX_WORKERS_WRITE_CEILING

    fconc = (file_data.get("concurrency") or {})
    rconc = (running_data.get("concurrency") or {})
    file_mw = int(fconc.get("max_workers", 2) or 2)
    file_en = bool(fconc.get("enabled", False))
    effective, warning = resolve_max_workers(file_data)
    restart = (
        file_mw != int(rconc.get("max_workers", 2) or 2)
        or file_en != bool(rconc.get("enabled", False))
    )
    return {
        "max_workers": file_mw,
        "enabled": file_en,
        "effective_max_workers": effective,
        "warning": warning,
        "max_allowed": _MAX_WORKERS_WRITE_CEILING,
        "restart_required": restart,
    }


@app.get("/api/config/workers")
async def api_get_config_workers(request: Request) -> dict[str, Any]:
    """How many tasks run at once — the on-disk value, the effective (clamped)
    pool, and whether a restart is pending. See :func:`_workers_payload`."""
    from ..config import CONFIG_PATH, load_config

    require_local_origin(request)
    cfg = getattr(request.app.state, "config", None)
    running_data = getattr(cfg, "data", None) or {}
    file_cfg = await asyncio.to_thread(load_config, CONFIG_PATH)
    return _workers_payload(file_cfg.data, running_data)


@app.put("/api/config/workers")
async def api_set_config_workers(request: Request) -> dict[str, Any]:
    """Change ``concurrency.max_workers`` and/or ``concurrency.enabled``.

    Body (JSON object, parsed by hand for a one-sentence 422 like the other
    ``/api/config/*`` writers): ``max_workers`` (int 1..64) and/or ``enabled``
    (bool). Validation and the atomic write both run through
    ``config.set_concurrency``. Does NOT reload ``app.state.config``: the pool
    size is bound at server start, so the change takes effect on the next
    ``nh serve`` — reloading here would make ``restart_required`` lie.
    """
    from ..config import CONFIG_PATH, AuthError, load_config, set_concurrency

    require_local_origin(request, writing=True)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — never surface the raw body
        raise HTTPException(422, "expected a JSON object") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "expected a JSON object")

    kwargs: dict[str, Any] = {}
    if "max_workers" in body:
        kwargs["max_workers"] = body["max_workers"]
    if "enabled" in body:
        kwargs["enabled"] = body["enabled"]
    if not kwargs:
        raise HTTPException(422, "expected 'max_workers' and/or 'enabled'")

    try:
        await asyncio.to_thread(set_concurrency, CONFIG_PATH, **kwargs)
    except (ValueError, AuthError) as exc:
        raise HTTPException(422, str(exc)) from exc

    cfg = getattr(request.app.state, "config", None)
    running_data = getattr(cfg, "data", None) or {}
    file_cfg = await asyncio.to_thread(load_config, CONFIG_PATH)
    return _workers_payload(file_cfg.data, running_data)


async def _attach_imported(
    request: Request, source: str, briefs: list[dict[str, Any]]
) -> list[TrackerIssueOut]:
    """SCRUM-18 — the accidental re-import trap, for either tracker.

    ONE local-store read (never a per-row store or tracker call) building an
    external_id -> [tasks] index, then an `imported` block on any row that
    already has a board task. A deleted board task simply isn't in the
    projection any more, so its ticket goes back to showing no chip — no stale
    reference is fabricated.

    `source` scopes the read, and that scoping is load-bearing now that two
    trackers are listed: dedupe keys on (source, external_id), so a Jira NO-1
    and a Linear NO-1 are different tickets and neither may claim the other's
    task. SCRUM-54: the narrow (external_id, id, status, created_at) projection,
    not a full `list_tasks()` hydration of every task just to read four fields.
    """
    imported_rows = await _store(request).list_imported_tasks(source)
    by_ext: dict[str, list] = {}
    for t in imported_rows:
        by_ext.setdefault(t.external_id, []).append(t)
    out = []
    for brief in briefs:
        row = TrackerIssueOut(**brief)
        matches = by_ext.get(brief.get("key"))
        if matches:
            # Same "latest task per external_id" definition as the sync
            # (jira_poll.sync_statuses): newest (created_at, id) — an older
            # import merely touched later must not win the chip.
            latest = max(matches, key=lambda t: (t.created_at, t.id))
            row.imported = ImportedInfo(
                task_id=latest.id, status=latest.status, count=len(matches),
            )
        out.append(row)
    return out


@app.get("/api/integrations/jira/issues", response_model=list[TrackerIssueOut])
async def jira_issues_endpoint(
    q: str = "", limit: int = 20, request: Request = None
) -> list[TrackerIssueOut]:
    """Free-text browse/pick over the configured Jira project — the read side
    of Task 1.6's "Import from Jira" affordance. This never creates a task;
    POST /api/tasks (with source="jira") stays the one create path. Reuses
    ``JiraAdapter`` from intake/jira.py completely — same auth, same search
    endpoint, same JIRA_API_TOKEN env var the background poller already uses.
    """
    from ..config import load_env_var
    from ..intake.jira import JiraAdapter

    cfg = request.app.state.config
    # Load JIRA_API_TOKEN from ~/.no_human/.env on demand (B1 pattern). Only the
    # `nh serve` poller loaded it at startup; under `nh start` (the board) it was
    # never in the process env, so the picker wrongly reported "not configured"
    # even with a valid token on file. JiraAdapter reads it from os.environ.
    load_env_var("JIRA_API_TOKEN")
    adapter = JiraAdapter(cfg.data)
    if not adapter.configured:
        raise HTTPException(
            status_code=503,
            detail="Jira is not configured — add it under Settings > Integrations.",
        )
    limit = max(1, min(limit, 50))
    try:
        issues = await asyncio.to_thread(adapter.search_text, q, limit)
    except httpx.HTTPError as exc:
        # Never surface the raw exception (it can carry the request URL/auth
        # object) — a short, tokenless detail only, and only the exception's
        # type name is ever logged.
        log.warning("jira issue search failed: %s", type(exc).__name__)
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            detail = (
                "Jira API token expired or invalid. Verify or rotate the "
                "token under Settings > Integrations."
            )
        else:
            detail = "Jira search failed — check the site/project configuration."
        raise HTTPException(status_code=502, detail=detail)
    # SCRUM-18 — accidental re-import trap: one local-store read (no per-row
    # Jira calls) building an external_id -> [tasks] index, then attach an
    # `imported` block to any issue that already has a board task. A deleted
    # board task simply isn't in this projection any more, so its ticket goes
    # back to showing no chip — no stale reference is fabricated.
    # SCRUM-54: a narrow (external_id, id, status, created_at) projection —
    # filtered to source='jira' AND external_id IS NOT NULL in SQL — replaces
    # the old list_tasks() full-Task hydration of every task in the store.
    return await _attach_imported(
        request, "jira", [adapter.issue_brief(i) for i in issues])


@app.get("/api/integrations/jira/issues/{key}", response_model=TrackerIssueOut)
async def jira_issue_detail_endpoint(key: str, request: Request) -> TrackerIssueOut:
    """Fetch ONE issue in full (SCRUM-9) — the detail GET behind the picker's
    "pick" action. ``/api/integrations/jira/issues`` (the browse list, above)
    truncates each description to 2000 chars for a small list payload; that
    truncation was leaking into created tasks because the web picker built
    its composer prefill straight from the list brief. This endpoint returns
    the SAME shape with the FULL description, so a picked issue's task can
    carry the whole spec instead of a cut-off one.
    """
    from ..config import load_env_var
    from ..intake.jira import JiraAdapter

    cfg = request.app.state.config
    load_env_var("JIRA_API_TOKEN")
    adapter = JiraAdapter(cfg.data)
    if not adapter.configured:
        raise HTTPException(
            status_code=503,
            detail="Jira is not configured — add it under Settings > Integrations.",
        )
    try:
        issue = await asyncio.to_thread(adapter.get_issue, key)
    except httpx.HTTPError as exc:
        log.warning("jira issue detail fetch failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="Jira lookup failed — check the site/project configuration.",
        )
    return TrackerIssueOut(**adapter.issue_detail(issue))


# --------------------------------------------------------------------------- #
# Linear — the SAME two routes, against the SAME adapter the poller uses.       #
#                                                                              #
# These did not exist, and the Backlog page told the operator why in a sentence #
# that was not true: "the Linear side has no issue listing yet". It has had one #
# the whole time — `LinearAdapter.search()` is a paginating GraphQL listing     #
# with a Relay cursor and a page bound. What was missing was only the HTTP      #
# route between it and the page. A UI that explains a gap with a fact about the #
# code has to be right about the code, so this closes the gap rather than       #
# rewording the sentence.                                                       #
# --------------------------------------------------------------------------- #

def _linear_adapter(request: Request):
    """The configured adapter, or a 503 that says what to fix.

    LINEAR_API_KEY is loaded from ~/.no_human/.env on demand — the B1 pattern
    the Jira routes above use, and for the same reason: only `nh serve`'s poller
    loads it at startup, so under `nh start` (the board) a perfectly configured
    integration reported "not configured" until the key was read here.
    """
    from ..config import load_env_var
    from ..intake.linear import LinearAdapter

    load_env_var("LINEAR_API_KEY")
    adapter = LinearAdapter(request.app.state.config.data)
    if not adapter.configured:
        raise HTTPException(
            status_code=503,
            detail="Linear is not configured — add it under Settings > Integrations.",
        )
    return adapter


def _linear_failure(exc: Exception, what: str) -> HTTPException:
    """One 502 for every Linear failure mode, with a tokenless detail.

    Linear does not classify by HTTP status — field errors arrive at 200, auth
    failure at 401, throttling at 400 — so the adapter's exception TYPE is the
    classification, not the status code. Only the exception's type name is ever
    logged: the message can quote the request, which carries the API key.
    """
    from ..intake.linear import LinearAuthError, LinearConfigError, LinearRateLimited

    log.warning("linear %s failed: %s", what, type(exc).__name__)
    if isinstance(exc, LinearConfigError):
        # The adapter builds this one itself, out of the operator's own config
        # and names the API returned — never out of a request — so it is the
        # one Linear failure whose message can be shown verbatim, and it is
        # the only one that tells the operator what to change. 503, not 502:
        # nothing is wrong upstream, the setting is wrong here.
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, LinearAuthError):
        detail = (
            "Linear API key rejected. Verify or rotate the key under "
            "Settings > Integrations."
        )
    elif isinstance(exc, LinearRateLimited):
        detail = "Linear is rate-limiting this workspace — try again in a minute."
    else:
        detail = "Linear lookup failed — check the team key and state types."
    return HTTPException(status_code=502, detail=detail)


@app.get("/api/integrations/linear/issues", response_model=list[TrackerIssueOut])
async def linear_issues_endpoint(
    q: str = "", limit: int = 20, request: Request = None
) -> list[TrackerIssueOut]:
    """Browse/pick over the configured Linear team's intake scope — the Linear
    half of the Backlog page's list. Never creates a task; POST /api/tasks
    (with source="linear") stays the one create path."""
    from ..intake.linear import LinearError

    adapter = _linear_adapter(request)
    limit = max(1, min(limit, 50))
    try:
        issues = await asyncio.to_thread(adapter.search_text, q, limit)
    except (LinearError, httpx.HTTPError) as exc:
        raise _linear_failure(exc, "issue search")
    return await _attach_imported(
        request, "linear", [adapter.issue_brief(i) for i in issues])


@app.get("/api/integrations/linear/issues/{key}", response_model=TrackerIssueOut)
async def linear_issue_detail_endpoint(key: str, request: Request) -> TrackerIssueOut:
    """ONE issue by its identifier ("NO-1"). Same contract as the Jira detail
    route so the page has one code path per row, whichever tracker it came
    from. 404 when the identifier is not in the configured intake scope —
    saying "not found" is honest; inventing an empty row is not."""
    from ..intake.linear import LinearError

    adapter = _linear_adapter(request)
    try:
        issue = await asyncio.to_thread(adapter.get_issue, key)
    except (LinearError, httpx.HTTPError) as exc:
        raise _linear_failure(exc, "issue detail fetch")
    if issue is None:
        raise HTTPException(
            status_code=404,
            detail=f"{key} is not in the configured Linear team's open issues.",
        )
    return TrackerIssueOut(**adapter.issue_detail(issue))


# --------------------------------------------------------------------------- #
# Onboarding wizard (web first-run). Reuses the existing onboard/history/      #
# learning logic — no parallel machinery.                                      #
#                                                                              #
# The derive/prove split is preserved, but BOTH halves are now reachable from  #
# the app: `/repos/onboard` derives (fast, one click) and `/repos/prove`       #
# streams a REAL run of the derived commands (`OnboardEngine`, the same engine #
# `nh onboard` drives), then `/repos/confirm` applies the same human gate the  #
# CLI applies (`onboard.confirm_profile`).                                     #
#                                                                              #
# Why this matters, stated so it is not re-broken: without a proven test       #
# command a task still RUNS — it just runs with no test command, so            #
# `runner.run_tests` falls back to `detect_command` and, when that finds        #
# nothing, reports "no tests run" as a non-failure. The PR still opens. The     #
# missing proof does not block the product; it hollows out the evidence the    #
# product's review gate is supposed to stand on. Proving in the wizard is      #
# about EVIDENCE, not about unblocking anyone.                                 #
# --------------------------------------------------------------------------- #

class RepoOnboardRequest(BaseModel):
    repo_path: str

class RepoProveRequest(BaseModel):
    """Prove a repo's commands by RUNNING them. The optional command fields are
    the human's correction after a failed attempt; each REPLACES that kind's
    derived candidates so we prove exactly the string the human typed."""
    repo_path: str
    test_cmd: str | None = None
    install_cmd: str | None = None
    lint_cmd: str | None = None
    timeout: int = 1800

class RepoConfirmRequest(BaseModel):
    repo_path: str

class RepoUiEvidenceRequest(BaseModel):
    """The wizard's one-action confirm for the ui_evidence suggestion
    (no-human-67 follow-up). ``enabled`` is the human's Yes/No answer, never a
    client-supplied start_cmd/base_url — the server re-derives the suggestion
    itself so a stale or tampered client body can never write commands into a
    profile that get run later."""
    repo_path: str
    enabled: bool = False

class HistoryAnalyzeRequest(BaseModel):
    days: int = 30
    # Scope the scan to the repos the user selected (spec §3 B5). Empty = every
    # project on the machine, the pre-B5 behaviour a non-scoped caller keeps.
    repo_paths: list[str] = []

class ConfirmRulesRequest(BaseModel):
    ids: list[str] = []

class OnboardingCompleteRequest(BaseModel):
    team: str | None = None
    repos: list[str] = []
    docs: list[str] = []
    telemetry_asked: bool = False
    # Minimal path (spec §3 B1): finish after picking one repo. The server
    # creates a project named after the repo and records the skipped steps as
    # deferred so the board's Finish-setup card can carry them.
    minimal: bool = False
    repo_path: str | None = None


# The steps the minimal path skips, in the order the Finish-setup card lists them.
DEFERRED_STEPS = ["docs", "integrations", "history", "rules"]


def _read_onboarding(config) -> dict[str, Any]:
    return dict((config.data.get("onboarding") or {}))


def _persist_onboarding(config, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into config.onboarding, in memory AND on disk (config.yaml).
    Mirrors how cli/init_cmd writes config — no secrets are touched here."""
    import yaml
    from ..config import CONFIG_PATH
    ob = dict((config.data.get("onboarding") or {}))
    ob.update(patch)
    config.data["onboarding"] = ob
    try:
        on_disk = yaml.safe_load(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except Exception:  # noqa: BLE001
        on_disk = {}
    on_disk = on_disk or {}
    on_disk["onboarding"] = ob
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(CONFIG_PATH, yaml.safe_dump(on_disk, sort_keys=False))
    except OSError as exc:
        log.warning("could not persist onboarding to %s: %s", CONFIG_PATH, exc)
    return ob


@app.get("/api/fs/suggest")
async def fs_suggest(path: str = "") -> dict[str, Any]:
    """Directory autocomplete for path inputs. Given a partial path, return up to
    20 matching sub-directories (absolute, ~-expanded). Used by the onboarding
    and composer repo/docs inputs to autofill as the user types.

    It lists NAMES via ``iterdir`` only and never stats ``<dir>/.git``: doing
    that inside ``~/Documents`` or ``~/Desktop`` is what raised the macOS "wants
    to access" prompt during setup. For the same reason, when the base directory
    IS the user's home, the TCC-guarded folders (:data:`PROTECTED_HOME_DIRS`)
    are not offered at all — a repo does not live in Downloads.
    """
    from ..repo_discovery import PROTECTED_HOME_DIRS

    raw = (path or "").strip() or "~"
    expanded = Path(raw).expanduser()
    # If the user is mid-typing a segment (no trailing slash and the path isn't a
    # dir), complete against the parent using the last segment as a prefix.
    if raw.endswith("/") or expanded.is_dir():
        base, prefix = expanded, ""
    else:
        base, prefix = expanded.parent, expanded.name.lower()
    hidden = set(PROTECTED_HOME_DIRS) if base == Path.home() else set()
    out: list[dict[str, Any]] = []
    try:
        for p in sorted(base.iterdir()):
            if not p.is_dir() or p.name.startswith(".") or p.name in hidden:
                continue
            if prefix and not p.name.lower().startswith(prefix):
                continue
            out.append({"path": str(p), "name": p.name})
            if len(out) >= 20:
                break
    except OSError:
        pass
    return {"base": str(base), "suggestions": out}


@app.get("/api/repos/discover")
async def discover_repositories(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=1000),
    root: str | None = Query(default=None),
) -> dict[str, Any]:
    """Find the user's repositories so onboarding and the composer can offer a
    list instead of demanding a typed path.

    The default scan is bound to the process's own home directory (home itself
    plus the conventional clone roots) and whatever the operator put in
    ``onboarding.extra_scan_roots``. ``root`` is NOT an arbitrary-filesystem
    escape hatch: :func:`discover_repos` refuses any ``root`` that resolves
    outside home, exactly as it does for the configured extra roots. It is the
    "type a folder to scan just that folder" path that replaced the old,
    unbounded ``POST /api/onboarding/repos/detect`` scanner.
    """
    from ..repo_discovery import DEFAULT_MAX_RESULTS, discover_repos

    ob = (request.app.state.config.data.get("onboarding") or {})
    extra = ob.get("extra_scan_roots") or []
    if isinstance(extra, str):
        extra = [extra]
    return await asyncio.to_thread(
        discover_repos,
        extra_roots=list(extra),
        max_results=limit if limit is not None else DEFAULT_MAX_RESULTS,
        root=root,
    )


@app.get("/api/onboarding/status")
async def onboarding_status(request: Request) -> dict[str, Any]:
    ob = _read_onboarding(request.app.state.config)
    return {"completed": bool(ob.get("completed")), **ob}


@app.post("/api/onboarding/repos/onboard")
async def onboarding_onboard_repo(
    body: RepoOnboardRequest, request: Request
) -> dict[str, Any]:
    """Derive a ProjectProfile from the repo's declarations and persist it
    UNPROVEN. Proving (running the test suite) is intentionally deferred to
    `nh onboard <repo>` so a click here never blocks on a long test run."""
    store = _store(request)
    config = request.app.state.config
    repo = Path(body.repo_path).expanduser().resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise HTTPException(422, f"{body.repo_path!r} is not a git repository")

    from ..onboard import DeclarationDeriver, derive_required_credentials, OnboardEngine
    from ..profile import ProjectProfile

    derived = await asyncio.to_thread(DeclarationDeriver().derive, repo)
    vcs_host, vcs_remote = await asyncio.to_thread(OnboardEngine._derive_vcs, repo)

    def _first(kind: str) -> str:
        cands = derived.of_kind(kind)
        return cands[0].command if cands else ""

    github_hosts = (config.data.get("git") or {}).get("github_hosts") or ["github.com"]
    # Re-profiling must not destroy verified state. A proof attests that ONE
    # exact command string exited clean in this cwd (onboard.py's prove
    # contract), so when re-derivation lands on the SAME commands the old
    # proof still holds — carry it forward. Any changed command resets to
    # unproven, because the proof no longer describes what would run.
    # Observed 2026-08-17: "Profile N repos" silently wiped proven+confirmed
    # for every already-proven repo it re-derived.
    prior = await store.get_profile(str(repo))
    # Carry each command's proof forward PER-COMMAND — keep a proof only when
    # THAT exact command is unchanged. The old all-or-nothing carry wiped every
    # proof (incl. the review gate's test proof) whenever any one command
    # changed — and re-derivation flips install `npm install`->`npm ci` once the
    # prove run has created a lockfile, so re-onboarding a proven repo silently
    # lost its test command. Per-command carry preserves the unchanged test
    # proof through an unrelated install-command flip.
    prior_proven = dict(getattr(prior, "proven", None) or {}) if prior else {}
    carried_proven = {}
    if prior:
        if prior.install_cmd == _first("install") and prior_proven.get("install_cmd"):
            carried_proven["install_cmd"] = prior_proven["install_cmd"]
        if prior.test_cmd == _first("test") and prior_proven.get("test_cmd"):
            carried_proven["test_cmd"] = prior_proven["test_cmd"]
        if prior.lint_cmd == _first("lint") and prior_proven.get("lint_cmd"):
            carried_proven["lint_cmd"] = prior_proven["lint_cmd"]
    # `confirmed` (the human's "use this repo") holds while the test command it
    # was confirmed against is still proven; otherwise the gate has nothing to run.
    carry_confirmed = bool(prior and prior.confirmed and carried_proven.get("test_cmd"))
    # ui_evidence must survive a re-derive the same way proofs do: this
    # endpoint builds a brand-new ProjectProfile on every call, and
    # store.upsert_profile REPLACES the whole DB row from it — omitting
    # ui_evidence here would silently wipe a manually-configured (or
    # previously offered-and-accepted) ui_evidence block on every re-onboard.
    carry_kwargs: dict[str, Any] = {}
    if prior:
        carry_kwargs["ui_evidence"] = dict(prior.ui_evidence)
    profile = ProjectProfile(
        repo_path=str(repo),
        ecosystem=derived.ecosystem,
        install_cmd=_first("install"),
        test_cmd=_first("test"),
        lint_cmd=_first("lint"),
        ci=derived.ci,
        human_gated_steps=derived.human_gated_steps,
        vcs_host=vcs_host,
        vcs_remote=vcs_remote,
        required_credentials=derive_required_credentials(
            derived.ci, vcs_host, derived.human_gated_steps, github_hosts),
        derived_from=sorted(set(derived.sources)),
        proven=carried_proven,
        confirmed=carry_confirmed,
        notes=(prior.notes if (prior and carried_proven) else
               "derived in onboarding wizard (unproven — prove it to give the "
               "review gate a test command to run)"),
        **carry_kwargs,
    )
    await store.upsert_profile(profile)

    from ..onboard import ui_evidence_suggestion

    sug = ui_evidence_suggestion(profile, str(repo))
    return {
        "ok": True,
        "repo_path": str(repo),
        "ecosystem": profile.ecosystem,
        "install_cmd": profile.install_cmd,
        "test_cmd": profile.test_cmd,
        "lint_cmd": profile.lint_cmd,
        "required_credentials": profile.required_credentials,
        "proven": bool(profile.proven.get("test_cmd")),
        "ui_evidence": {
            "configured": bool(profile.ui_evidence.get("enabled")
                                or profile.ui_evidence.get("start_cmd")
                                or profile.ui_evidence.get("base_url")),
            "enabled": bool(profile.ui_evidence.get("enabled")),
            "start_cmd": profile.ui_evidence.get("start_cmd") or "",
            "base_url": profile.ui_evidence.get("base_url") or "",
            "suggestion": sug,
        },
    }


def _profile_readiness(prof: Any) -> dict[str, Any]:
    """The one shape the whole app uses to describe how far a repo profile got
    up the trust ladder. ``is_usable`` is READ from ``ProjectProfile`` — never
    recomputed here, so no surface can disagree with the orchestrator's gate."""
    return {
        "repo_path": prof.repo_path,
        "name": (prof.repo_path or "").rstrip("/").rsplit("/", 1)[-1],
        "ecosystem": prof.ecosystem,
        "install_cmd": prof.install_cmd,
        "test_cmd": prof.test_cmd,
        "lint_cmd": prof.lint_cmd,
        "proven": dict(prof.proven or {}),
        "test_proven": bool((prof.proven or {}).get("test_cmd")),
        "confirmed": bool(prof.confirmed),
        "is_usable": bool(prof.is_usable),
    }


@app.get("/api/onboarding/readiness")
async def onboarding_readiness(request: Request) -> dict[str, Any]:
    """Which onboarded repos can back a task with REAL test evidence.

    This is what the summary step and the board banner read, so neither can
    claim "Ready." while every profile is unproven. A repo missing from
    ``usable`` is not blocked — its tasks will run — but its review gate will
    have no test command to execute, which is the thing worth saying out loud.
    """
    store = _store(request)
    try:
        rows = await store.list_profiles()
    except Exception:  # noqa: BLE001 — table may not exist yet
        rows = []
    from ..profile import ProjectProfile
    repos = [_profile_readiness(ProjectProfile.from_dict(json.loads(r["data"])))
             for r in rows if r.get("data")]
    usable = [r for r in repos if r["is_usable"]]
    return {
        "repos": repos,
        "total": len(repos),
        "usable": len(usable),
        "needs_proving": [r for r in repos if not r["is_usable"]],
        "first_usable": usable[0]["repo_path"] if usable else None,
    }


@app.post("/api/onboarding/repos/prove")
async def onboarding_prove_repo(body: RepoProveRequest, request: Request):
    """PROVE a repo's derived commands by actually RUNNING them, streaming the
    real output back as SSE so the user watches the thing that decides.

    This is the same `OnboardEngine` `nh onboard` drives — not a second
    implementation — so the command proven here is byte-for-byte the command
    `runner.run_tests` executes for the orchestrator later. Nothing in this
    endpoint can create a proof: it only reports the exit status of a real
    subprocess, and it always persists the profile UNCONFIRMED. Confirming is a
    separate human act (`/repos/confirm`).

    A failing command is a legitimate outcome, not an error: the stream reports
    it with its output and the caller may re-POST with a corrected `test_cmd`.
    """
    store = _store(request)
    config = request.app.state.config
    repo = Path(body.repo_path).expanduser().resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise HTTPException(422, f"{body.repo_path!r} is not a git repository")

    from ..onboard import DeclarationDeriver, OnboardEngine

    github_hosts = (config.data.get("git") or {}).get("github_hosts") or ["github.com"]
    overrides = {"test": body.test_cmd or "", "install": body.install_cmd or "",
                 "lint": body.lint_cmd or ""}
    timeout = max(30, min(int(body.timeout or 1800), 7200))

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _emit(frame: dict[str, Any] | None) -> None:
        # Called from the prove worker thread (each output line) as well as from
        # the loop, so it must hop threads explicitly.
        loop.call_soon_threadsafe(queue.put_nowait, frame)

    async def _run_prove() -> None:
        try:
            engine = OnboardEngine(
                DeclarationDeriver(), prove_timeout=timeout,
                github_hosts=github_hosts, on_event=_emit,
            )
            result = await engine.onboard(repo, overrides=overrides)
            prof = result.profile
            # Never inherit an earlier confirm: the command may have changed, so
            # the human re-confirms against THIS evidence.
            prof.confirmed = False
            await store.upsert_profile(prof)
            try:
                prof.save()
            except OSError as exc:
                log.warning("could not write project.yml for %s: %s", repo, exc)
            _emit({
                "kind": "done",
                **_profile_readiness(prof),
                "proofs": [
                    {"kind": p.kind, "command": p.command, "ok": p.ok,
                     "exit_code": p.exit_code, "output": (p.output or "")[-4000:]}
                    for p in result.proofs
                ],
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("prove failed for %s: %s", repo, type(exc).__name__)
            _emit({"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            _emit(None)  # sentinel

    async def _generate():
        task = asyncio.create_task(_run_prove())
        started = time.monotonic()
        try:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    # A quiet suite is normal (compiling, installing). Say so
                    # with an elapsed count rather than leaving a dead spinner.
                    yield ("data: " + json.dumps({
                        "kind": "heartbeat",
                        "elapsed": int(time.monotonic() - started),
                    }) + "\n\n")
                    continue
                if frame is None:
                    yield "data: {\"kind\": \"stream_end\"}\n\n"
                    return
                yield f"data: {json.dumps(frame)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/onboarding/repos/confirm")
async def onboarding_confirm_repo(
    body: RepoConfirmRequest, request: Request
) -> dict[str, Any]:
    """The human gate, from the app: mark a PROVEN profile confirmed.

    Delegates the decision to ``onboard.confirm_profile`` — the same function
    `nh onboard --confirm` calls — so the GUI can never confirm something the
    CLI would refuse. An unproven profile is rejected here; the remedy is to
    prove it, never to relax this.
    """
    store = _store(request)
    repo = Path(body.repo_path).expanduser().resolve()

    from ..onboard import ProfileNotProven, confirm_profile
    from ..profile import ProjectProfile

    prof = await store.get_profile(str(repo)) or ProjectProfile.load(repo)
    if prof is None:
        raise HTTPException(404, f"no profile for {body.repo_path!r} — onboard it first")
    try:
        confirm_profile(prof)
    except ProfileNotProven as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        prof.save()
    except OSError as exc:
        log.warning("could not write project.yml for %s: %s", repo, exc)
    await store.upsert_profile(prof)
    return {"ok": True, **_profile_readiness(prof)}


@app.post("/api/onboarding/repos/ui-evidence")
async def onboarding_ui_evidence(
    body: RepoUiEvidenceRequest, request: Request
) -> dict[str, Any]:
    """The wizard's one-action confirm for the ui_evidence suggestion
    (no-human-67 follow-up): declining writes nothing, accepting writes
    ``ui_evidence`` to BOTH project.yml and the DB row via the standard
    profile-persist path.

    The suggestion is always re-derived HERE from the repo's own
    package.json, never trusted from the request body — ``body.enabled`` is
    the only thing the client controls. A profile that is already manually
    configured (``ui_evidence_suggestion`` returns ``None`` in that case) is
    left untouched: manual config always wins, this never re-prompts or
    overwrites it.
    """
    store = _store(request)
    repo = Path(body.repo_path).expanduser().resolve()

    from ..onboard import (
        ProjectYmlPersistError, apply_ui_evidence_suggestion, persist_profile,
        ui_evidence_suggestion,
    )
    from ..profile import ProjectProfile

    prof = await store.get_profile(str(repo)) or ProjectProfile.load(repo)
    if prof is None:
        raise HTTPException(404, f"no profile for {body.repo_path!r} — onboard it first")

    sug = ui_evidence_suggestion(prof, str(repo))
    if not body.enabled:
        # Decline: no writes at all, regardless of whether a suggestion
        # exists — "not now" must be a true no-op.
        return {"ok": True, "enabled": False, "ui_evidence": dict(prof.ui_evidence)}
    if sug is None:
        raise HTTPException(
            422,
            f"no `npm run dev` convention detected for {body.repo_path!r} "
            "(or ui_evidence is already configured) — nothing to enable",
        )
    apply_ui_evidence_suggestion(prof, sug)
    try:
        await persist_profile(store, prof)
    except ProjectYmlPersistError as exc:
        # Neither artifact may be reported as configured when only one of
        # them could be written — persist_profile already skipped the DB
        # write to avoid exactly that split-brain state.
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, "enabled": True, "ui_evidence": dict(prof.ui_evidence)}


async def _gather_history(
    days: int, repo_paths: list[str] | None = None,
) -> tuple[list, dict[str, int]]:
    """Combine conversation history from every available source: the IDE
    transcript extractor (best-effort — needs a running IDE) AND Claude Code
    (read from disk, always available). Returns (transcripts, per-source counts).

    ``repo_paths`` scopes the Claude Code scan to the selected repos (spec §3
    B5) — None/empty keeps every project, as before."""
    from ..history.extractor import extract_transcripts, IDENotRunningError
    from ..history.claude_code import extract_claude_code_transcripts, _cwd_under
    from ..history.analyzer import _project_from_workspaces

    transcripts: list = []
    sources: dict[str, int] = {}
    try:
        ws = await asyncio.to_thread(extract_transcripts, days=days)
        if repo_paths:
            # The IDE extractor is machine-wide (every workspace); the CC scan is
            # already repo-scoped. Without this a repo-focused onboarding pulled
            # conversations from EVERY workspace (feedback I4). Keep a transcript
            # only if its workspace project sits under a selected repo. A
            # transcript with no derivable project is DROPPED here — it can't be
            # shown to belong to the focused repo. repo_paths empty => unchanged.
            ws = [t for t in ws
                  if _cwd_under(_project_from_workspaces(t.workspaces), repo_paths)]
        transcripts += ws
        sources["windsurf"] = len(ws)  # term-ok: internal source tag names the real IDE
    except IDENotRunningError:
        sources["windsurf"] = 0  # term-ok: internal source tag names the real IDE
    except Exception as exc:  # noqa: BLE001
        log.warning("Windsurf extract failed: %s", exc)  # term-ok: real IDE name
        sources["windsurf"] = 0  # term-ok: internal source tag names the real IDE
    try:
        cc = await asyncio.to_thread(
            extract_claude_code_transcripts, days=days, repo_paths=repo_paths)
        transcripts += cc
        sources["claude_code"] = len(cc)
    except Exception as exc:  # noqa: BLE001
        log.warning("Claude Code extract failed: %s", exc)
        sources["claude_code"] = 0
    return transcripts, sources


@app.post("/api/onboarding/history/extract")
async def onboarding_history_extract(request: Request) -> dict[str, Any]:
    """Count extractable transcripts across every available source (the IDE
    transcript extractor and Claude Code) plus the user's skills. Honest when a
    source is empty — an empty source reports zero, never fabricated data."""
    from ..history.skills import discover_skills
    transcripts, sources = await _gather_history(30)
    skills = await asyncio.to_thread(discover_skills)
    return {
        "available": bool(transcripts) or bool(skills),
        "transcripts": len(transcripts),
        "messages": sum(len(t.messages) for t in transcripts),
        "sources": sources,
        "skills": len(skills),
        "detail": "no Windsurf IDE and no Claude Code history found"  # term-ok: real IDE name (user-facing)
                  if not transcripts else "",
    }


@app.post("/api/onboarding/history/analyze")
async def onboarding_history_analyze(
    body: HistoryAnalyzeRequest, request: Request
) -> dict[str, Any]:
    """Extract transcripts → analyze for corrections → propose each into the
    human-confirmed learning queue (confirmed=0). Nothing becomes an active rule
    until confirmed — preserving the learning-queue invariant."""
    store = _store(request)
    from ..history.ingester import TranscriptIngester
    transcripts, sources = await _gather_history(body.days, body.repo_paths)
    messages = sum(len(t.messages) for t in transcripts)

    # Build an LLM-distillation pass so proposed rules are GENERAL, durable
    # lessons (importance-labelled) rather than raw matched user messages — and
    # one-off task requests get filtered out. Uses the cheaper review model at
    # low effort, read-only. If the backend/auth is unavailable the ingester
    # falls back to the heuristic pass (still works), so this never hard-fails.
    config = request.app.state.config
    llm_call = None
    try:
        from ..agent.claude_backend import ClaudeBackend
        _b = ClaudeBackend(model=config.review_model, readonly=True)

        async def llm_call(prompt: str) -> str:  # noqa: F811
            res = await _b.run(prompt, cwd=Path.cwd(), max_turns=1, effort="low")
            return res.final_text or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM analyzer unavailable, heuristic-only: %s", exc)

    # Route through the standalone ingester (EVOLUTION_PLAN §1.1) so the web
    # wizard, the CLI, and periodic re-analysis all share one code path. It
    # enqueues every finding as source="proposed"/confirmed=0 with a stable
    # dedupe_key (idempotent) — nothing activates until a human confirms it.
    ingester = TranscriptIngester(store, llm_call=llm_call)
    result = await ingester.ingest_transcripts(transcripts, use_llm=llm_call is not None)
    proposals = list(result.proposals)

    # Also catalog the user's Claude Code skills as proposed `skill` memories —
    # so the rules-review shows them and (once confirmed) the Supervisor's
    # "skill-exists" detector knows they exist (EVOLUTION_PLAN §1.3 row 1).
    from ..history.skills import discover_skills
    from ..learning.pii import contains_pii
    skills_added = 0
    for s in await asyncio.to_thread(discover_skills):
        # Same personal-data gate as the mined findings — a skill's name or
        # description is user-authored text and reaches the same queue.
        if contains_pii(s.name, s.description or "") is not None:
            continue
        mid = await store.add_memory(
            mem_type="skill", title=s.name, content=s.description or s.name,
            tags=["skill", "claude_code"], source="proposed", confirmed=False,
            dedupe_key=f"skill:{s.name}",
        )
        if mid:
            skills_added += 1
            # Skills are machine-wide, not repo-scoped: empty project, so the
            # web step groups them as in-scope rather than under "other".
            proposals.append({"id": mid, "category": "skill", "title": s.name,
                              "content": s.description or s.name,
                              "importance": "med", "project": ""})

    # NOTHING IS PRE-SELECTED. A real user was shown their own home address and
    # phone number already TICKED for confirmation as standing guidance — one
    # click from becoming an active rule. Confirmation is opt-in per memory:
    # the server states the default explicitly rather than leaving it to the
    # client to decide, so any client (SPA, future CLI/TUI, a third-party one)
    # inherits opt-in rather than re-inventing pre-ticking.
    for p in proposals:
        p["selected"] = False

    return {"available": True, "proposed": result.proposed + skills_added,
            "duplicates": result.duplicates, "messages": messages,
            "sources": sources, "skills": skills_added,
            "dropped_pii": result.dropped_pii,
            "default_selected": False,
            "transcripts": result.transcripts, "proposals": proposals}


@app.post("/api/onboarding/rules/confirm")
async def onboarding_confirm_rules(
    body: ConfirmRulesRequest, request: Request
) -> dict[str, Any]:
    """Confirm selected proposed learnings → they become active rules. Reuses
    the existing LearningQueue.confirm (the only path that activates a rule)."""
    store = _store(request)
    from ..learning import LearningQueue
    q = LearningQueue(store)
    confirmed = 0
    for mem_id in body.ids:
        m = await store.find_memory(mem_id)
        if m and await q.confirm(m["id"]):
            confirmed += 1
    return {"ok": True, "confirmed": confirmed}


@app.post("/api/onboarding/complete")
async def onboarding_complete(
    body: OnboardingCompleteRequest, request: Request
) -> dict[str, Any]:
    config = request.app.state.config
    prior = _read_onboarding(config)
    patch = {
        "completed": True,
        "completed_at": _now(),
        "team": body.team,
        "repos": body.repos,
        "docs": body.docs,
    }
    # Minimal path: finish after one repo. Create the project server-side (the
    # frontend cannot build a repo-less project this way) and record the steps
    # the user skipped so the board can offer them.
    if body.minimal:
        if not body.repo_path:
            raise HTTPException(400, "minimal completion requires repo_path")
        await _ensure_project_for_repo(_store(request), body.repo_path)
        patch["deferred"] = list(DEFERRED_STEPS)
        patch["repos"] = [str(Path(body.repo_path).expanduser().resolve())]
    # Sticky: once asked, never un-asked — a re-run of the wizard (via
    # onboarding/reset) must not resurrect the telemetry question.
    if body.telemetry_asked or prior.get("telemetry_asked"):
        patch["telemetry_asked"] = True
    ob = _persist_onboarding(config, patch)
    return {"ok": True, "onboarding": ob}


async def _ensure_project_for_repo(store: Store, repo_path: str) -> None:
    """Create a project named after the repo bound to it, unless one already
    binds that repo. Reuses the same Project.new + store.create_project path as
    the POST /api/projects endpoint (api_create_project)."""
    resolved = str(Path(repo_path).expanduser().resolve())
    existing = await store.list_projects()
    if any(resolved in p.repo_paths for p in existing):
        return
    from ..project_model import Project
    name = Path(resolved).name
    proj = Project.new(name=name, repo_paths=[resolved], primary_repo=resolved)
    try:
        await store.create_project(proj)
    except Exception as exc:  # noqa: BLE001
        # A name collision (a different repo already owns this basename) is not
        # fatal to onboarding — the user still gets to the board.
        if "UNIQUE" not in str(exc):
            raise


@app.get("/api/onboarding/deferred")
async def onboarding_deferred(request: Request) -> dict[str, Any]:
    ob = _read_onboarding(request.app.state.config)
    return {"deferred": list(ob.get("deferred") or [])}


@app.post("/api/onboarding/deferred/{step}/done")
async def onboarding_deferred_done(step: str, request: Request) -> dict[str, Any]:
    config = request.app.state.config
    remaining = [s for s in (_read_onboarding(config).get("deferred") or []) if s != step]
    ob = _persist_onboarding(config, {"deferred": remaining})
    return {"deferred": list(ob.get("deferred") or [])}


@app.post("/api/onboarding/reset")
async def onboarding_reset(request: Request) -> dict[str, Any]:
    """Show the setup wizard again. Clears `completed` and NOTHING else.

    There was no way back into onboarding: `completed` was written True in one
    place and False nowhere, so a user who blew through the eight steps (none of
    which gate) and landed in a wrong state — no proven repo, no projects,
    history never scanned — had one route, hand-editing config.yaml and
    restarting the server. The wizard is the screen that fixes all of those and
    it removed itself.

    The reset patch is deliberately one key (`completed`): repos, profiles,
    projects, confirmed rules, docs and the persisted `team` value (config only,
    not a wizard step — the step itself left the free-tier wizard) all survive
    the reset itself, untouched in memory and on disk. But the wizard FORM does
    not read any of that back: `Onboarding.jsx` starts repo, docs and project
    selections as empty React state and posts `team: null`, so if the user
    re-completes the wizard afterwards, `onboarding_complete` overwrites
    `team`/`repos`/`docs` with whatever the fresh form holds. The reset alone
    loses nothing; a full re-completion afterwards can.

    The board reads this flag once, at load: the desktop's File → "Re-run Setup…"
    resets and reloads the window; a caller hitting this endpoint on its own has
    to reload the board itself.
    """
    ob = _persist_onboarding(request.app.state.config, {"completed": False})
    return {"completed": bool(ob.get("completed")), **ob}


class DocsGenerateRequest(BaseModel):
    repo_path: str


@app.post("/api/onboarding/docs/generate", status_code=202)
async def onboarding_docs_generate(
    body: DocsGenerateRequest, request: Request
) -> dict[str, Any]:
    """Queue wiki generation as a background job; return 202 + the job id.

    The generation is a bounded Agent SDK session that can take minutes. It runs
    in a detached task so the wizard is not blocked and the result survives the
    wizard unmounting; poll ``GET /api/onboarding/docs/jobs/{id}``. Backend
    construction happens INSIDE the task so a config problem fails the job (which
    the board shows) rather than 500ing this request.
    """
    from ..wiki_jobs import run_job
    from ..core.db import _now

    store = request.app.state.store
    config = request.app.state.config
    job_id = await store.create_wiki_job(body.repo_path)

    async def _bg() -> None:
        from ..docs_gen import WikiGenerator
        from ..agent.claude_backend import ClaudeBackend
        try:
            backend = ClaudeBackend(
                model=config.primary_model,
                forbidden_paths=config["safety"]["forbidden_paths"],
            )
            gen = WikiGenerator(backend, max_turns=12)
        except Exception as exc:  # noqa: BLE001 — bad config is a failed job, not a crash
            await store.update_wiki_job(
                job_id, status="failed",
                error=f"backend init failed: {exc}", finished_at=_now())
            return
        await run_job(store, job_id, gen)

    asyncio.create_task(_bg())
    return {"job_id": job_id}


@app.get("/api/onboarding/docs/jobs/{job_id}")
async def onboarding_docs_job(job_id: str, request: Request) -> dict[str, Any]:
    """Poll one wiki job."""
    row = await request.app.state.store.get_wiki_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such wiki job")
    return row


@app.get("/api/onboarding/docs/jobs")
async def onboarding_docs_jobs(
    request: Request, status: str | None = Query(default=None)
) -> dict[str, Any]:
    """List wiki jobs, newest first; optionally filtered by status."""
    return {"jobs": await request.app.state.store.list_wiki_jobs(status=status)}


@app.get("/api/onboarding/docs/detect")
async def onboarding_docs_detect(repo: str) -> dict[str, Any]:
    """The docs a coder already reads: README.md / docs/ / CONTRIBUTING.md, if
    present. Informational chips for the wizard — no generation, no cost."""
    from pathlib import Path
    root = Path(repo).expanduser()
    found = [name for name in ("README.md", "docs", "CONTRIBUTING.md")
             if (root / name).exists()]
    return {"repo": repo, "found": found}


# --------------------------------------------------------------------------- #
# WebSocket — live board (polls DB every 2 s, broadcasts on change)           #
# --------------------------------------------------------------------------- #

def _task_fingerprint(tlist) -> dict:
    """B2 #10: the old (status, updated_at, live_status) tuple missed card
    fields (subtask_progress, pr_url, attempt_count, cancelled) — a subtask
    completing bumps only the CHILD row, so the parent card stayed stale
    forever (full-snapshot pushes can't repair what never sends). Hash the
    whole summary payload instead."""
    return {t.id: hash(json.dumps(t.model_dump(), sort_keys=True, default=str))
            for t in tlist}


@app.websocket("/ws")
async def ws_board(ws: WebSocket) -> None:
    if not ws_handshake_is_local(ws.headers, allowed_hosts(ws.app)):  # bypasses CORS + middlewares
        await ws.close(code=1008)  # policy violation
        return
    await _mgr.connect(ws)
    store: Store = ws.app.state.store
    sched = getattr(ws.app.state, "scheduler", None)
    try:
        # Initial snapshot.
        tasks = await _board_tasks(store, scheduler=sched)
        await _mgr.send(ws, json.dumps({
            "type": "init",
            "tasks": [t.model_dump() for t in tasks],
        }))
        # Sync loop: any change in the FULL summary payload pushes (B2 #10).
        # The client never sends, so a completed receive() means DISCONNECT —
        # awaited alongside the poll pause, or an idle board (nothing to send,
        # nothing to raise) leaked this task polling the store every 2s per
        # closed tab, forever (PR #109 review, proven empirically).
        prev_fp = _task_fingerprint(tasks)
        recv = asyncio.ensure_future(ws.receive())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {recv}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
                if recv in done:
                    return  # client closed (or spoke) — the finally cleans up
                tasks = await _board_tasks(store, scheduler=sched)
                curr_fp = _task_fingerprint(tasks)
                if curr_fp != prev_fp:
                    sched = getattr(ws.app.state, "scheduler", None)
                    worker = {"inflight": len(sched.inflight) if sched else 0}
                    await _mgr.send(ws, json.dumps({
                        "type": "sync",
                        "tasks": [t.model_dump() for t in tasks],
                        "worker": worker,
                    }))
                    prev_fp = curr_fp
        finally:
            recv.cancel()
            # The normal-disconnect RETURN path skipped _mgr.remove, so closed
            # tabs accumulated inert socket+lock entries until the next
            # broadcast pruned them (PR #109 round-2, low). remove() is
            # idempotent — the except paths below stay correct.
            _mgr.remove(ws)
    except WebSocketDisconnect:
        _mgr.remove(ws)
    except Exception:  # noqa: BLE001
        # B2 #9: remove-without-close left the CLIENT's onclose unfired — the
        # board froze while still showing "Connected". Close so it reconnects.
        _mgr.remove(ws)
        with contextlib.suppress(Exception):
            await ws.close()


# --------------------------------------------------------------------------- #
# Serve the React SPA (if built)                                               #
# --------------------------------------------------------------------------- #

if (_WEB_DIST / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=str(_WEB_DIST / "assets")), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str = "") -> FileResponse:
        # Never intercept /api/ or /ws paths — those are backend routes.
        # If they reach here, it means the route doesn't exist (404).
        if path.startswith("api/") or path.startswith("ws"):
            return PlainTextResponse(f"Not found: /{path}", status_code=404)
        # Vite copies `web/public/` to the ROOT of dist, not under /assets, so
        # a root-level static file (the brand mark, robots.txt, a manifest) is
        # outside the only mounted directory and would fall through to the app
        # shell. It did: the installed app answered /nh-mark-64.png with 601
        # bytes of index.html, so its own favicon was broken for every user
        # while every content check passed — the file was present, built and
        # bundled, and simply unreachable.
        #
        # resolve() then a parent check, because `path` is caller-controlled:
        # without it, `../../etc/passwd` reads outside the board directory.
        if path:
            candidate = (_WEB_DIST / path).resolve()
            try:
                inside = candidate.is_relative_to(_WEB_DIST.resolve())
            except AttributeError:                       # py<3.9
                inside = str(candidate).startswith(str(_WEB_DIST.resolve()))
            if inside and candidate.is_file():
                return FileResponse(str(candidate))
        # no-cache: index.html references content-hashed assets; without an
        # explicit header Chromium's HEURISTIC freshness serves a stale app
        # shell after every deploy (found live: the Electron shell ran a
        # bundle two deploys old while the e2e gate — which spins its own
        # static server — stayed green). Hashed /assets remain long-cacheable.
        return FileResponse(str(_WEB_DIST / "index.html"),
                            headers={"Cache-Control": "no-cache"})

else:
    # The board is missing. Before this branch existed the server simply had no
    # "/" route, so `nh start` — which README calls the primary entrypoint —
    # answered the browser with FastAPI's bare `{"detail":"Not Found"}` and the
    # user had no way to tell a broken install from a broken app. The API and
    # the worker are genuinely fine in this state, so this is not a hard
    # failure; it is a route that says which of the two situations it is and
    # what to do about it.
    log.warning(
        "board not found at %s — serving the API only. `nh start` will not "
        "render a UI. If this is a source checkout, build it with "
        "`cd web && npm install && npm run build`.", _WEB_DIST,
    )

    _NO_BOARD_MESSAGE = (
        "no_human: the web board is not installed.\n"
        "\n"
        f"Looked for index.html at: {_WEB_DIST}\n"
        "\n"
        "The API and the task worker are running normally — only the UI is\n"
        "missing, so the CLI works: `nh task`, `nh status`, `nh logs`,\n"
        "`nh approve`.\n"
        "\n"
        "To get the board:\n"
        "  * source checkout -> cd web && npm install && npm run build\n"
        "  * pip/uv install  -> this is a packaging bug, please report it;\n"
        "    a released wheel always ships the board.\n"
    )

    @app.get("/", include_in_schema=False)
    @app.get("/{path:path}", include_in_schema=False)
    async def spa_missing(path: str = "") -> PlainTextResponse:
        # Identical carve-out to the served case: /api/ and /ws are backend
        # routes, and a genuine 404 there must stay a plain 404 rather than be
        # answered with the board-missing notice.
        if path.startswith("api/") or path.startswith("ws"):
            return PlainTextResponse(f"Not found: /{path}", status_code=404)
        # 503, not 404: the resource is meant to exist and the deployment is
        # incomplete. A 404 reads as "wrong URL" and sends the user hunting.
        return PlainTextResponse(_NO_BOARD_MESSAGE, status_code=503)
