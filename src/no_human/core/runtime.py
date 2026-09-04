"""The ONE construction site for the coder orchestrator.

Both `nh` (CLI/TUI) and the FastAPI server build the coder `Orchestrator`
through `build_orchestrator` so `worker.backend` cannot diverge between the
two entry points (audit A8/X2, 2026-08-11): the server used to hardcode
`ClaudeBackend` in its own closure, so a task run through the GUI ignored
`worker.backend` while the same task via `nh` honoured it.
"""

from __future__ import annotations

from typing import Any

from ..agent.backend import make_backend
from ..context import ContextGatherer, build_default_sources
from ..notify import build_notifier
from .db import Store
from .orchestrator import Orchestrator


def task_backend_override(task: Any) -> str | None:
    """The backend THIS task asked for, or None to follow `worker.backend`.

    `nh task add --backend codex`, the API's `backend` field, and (since the
    board's coder-backend picker) the task composer all record the choice on
    `task.config["backend"]`. Until public issue #5 that value was
    only displayed; the coder still ran on the global key, so the flag looked
    like a per-task switch and was not one. Here it becomes one — for the
    CODER only: `make_backend` ignores any override for a non-coder role (see
    `CLAUDE_PINNED_ROLES`), and this module calls it with role="coder" only. An absent/blank value means "no
    opinion", so a task created before the flag existed is unchanged.
    """
    cfg = getattr(task, "config", None)
    if not isinstance(cfg, dict):
        return None
    value = str(cfg.get("backend") or "").strip().lower()
    return value or None


def assert_task_backend_usable(name: str | None, config: dict[str, Any] | None) -> None:
    """The per-task twin of the CLI's startup preflight for a GLOBAL backend.

    `nh start`/`nh serve` run `assert_codex_api_key_mode` /
    `assert_local_backend_mode` and the codex-CLI probe only when
    `worker.backend` names that backend (cli/commands.py). A task that picks
    codex or local on a claude-default install would otherwise skip all of it
    and die at its first coder turn, after planning tokens were spent — found
    by the independent review of this change. So the same assertions run here,
    at orchestrator construction, before any model call. Raises AuthError /
    BackendUnavailable naming `--backend`, never the global key.
    """
    if name == "codex":
        from ..agent.backend import BackendUnavailable
        from ..agent.codex_backend import find_codex_cli
        from ..config import assert_codex_mode, codex_auth_mode
        llm_cfg = (config or {}).get("llm") or {}
        assert_codex_mode(codex_auth_mode(config or {}), cli_path=llm_cfg.get("codex_cli_path"))
        if find_codex_cli(((config or {}).get("llm") or {}).get("codex_cli_path")) is None:
            raise BackendUnavailable(
                "this task asked for `--backend codex` but the `codex` CLI was "
                "not found. Install it (npm install -g @openai/codex) or file "
                "the task without --backend.")
    elif name == "local":
        from ..agent.backend import BackendUnavailable
        from ..config import assert_local_backend_mode
        llm_cfg = (config or {}).get("llm") or {}
        assert_local_backend_mode(llm_cfg.get("local_base_url"))
        if not str(llm_cfg.get("local_model") or "").strip():
            raise BackendUnavailable(
                "this task asked for `--backend local` but `llm.local_model` "
                "is not set. Set it in config.yaml: llm: local_model: <the "
                "model id the local server exposes>")


def build_orchestrator(config, store: Store, *, event_sink: Any = None,
                        task: Any = None) -> Orchestrator:
    # THE ONE SWITCH. `make_backend` returns exactly the ClaudeBackend this
    # line used to construct — same class, same arguments — unless
    # `worker.backend` says otherwise. The orchestrator below is handed a
    # `CodingBackend` and cannot tell which it got.
    task_backend = task_backend_override(task)
    assert_task_backend_usable(task_backend, config.data)
    backend = make_backend(
        model=config.primary_model,
        config=config.data,
        backend=task_backend,
        role="coder",
        forbidden_paths=config["safety"]["forbidden_paths"],
        never_push_to=config["git"]["never_push_to"],
    )
    # reviewer: AdversarialReviewer.from_config builds through
    # make_backend(role="reviewer") — default Claude claude-opus-4-8 readonly,
    # or the explicit llm.role_backends.reviewer choice.
    review_backend = None
    # Fan-out over every configured notify-OUT channel (Slack + Teams). One
    # source of truth for which channels are live: notify.build_notifier.
    notifier = build_notifier(config.data)
    gatherer = ContextGatherer(build_default_sources(store, config.data))
    from ..learning import LearningQueue
    from ..review.reviewer import AdversarialReviewer
    reviewer = AdversarialReviewer.from_config(config.data, backend=review_backend)
    # Memory lifecycle C: the per-success templated skill proposal is gated
    # behind this flag (default off — config.data merges unknown keys, so an
    # older config.yaml without it reads the DEFAULT_CONFIG default via
    # dict.get, never a KeyError).
    propose_on_success = bool(
        config.data.get("learning", {}).get("propose_on_success", False))
    return Orchestrator(store, config.data, backend, notifier,
                        event_sink=event_sink, context_gatherer=gatherer,
                        learning_queue=LearningQueue(
                            store, propose_on_success=propose_on_success),
                        reviewer=reviewer)
