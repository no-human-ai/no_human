"""A `backend=local` zero-token SDK death is INFRA, not QUOTA.

INCIDENT (2026-09-01, first-ever live ``backend=local`` run: a scratch server,
litellm's Anthropic-compat proxy over ollama). The local model returned HTTP
500 ("llama3 does not support thinking"), the SDK died at turn 1 with 0
tokens, and the orchestrator emitted ``paused_quota`` naming the "default"
subscription's reset — a subscription that does not exist in local mode:
``agent/backend.py``'s ``_local_child_env`` deliberately blanks
``CLAUDE_CODE_OAUTH_TOKEN`` so a local run never carries the operator's real
token to a third-party model server. The row then sat ``paused_quota``
waiting on a reset that could never come.

Follows ``tests/test_infra_not_work.py``'s exact idiom: a fake backend
returning a scripted ``AgentResult``, a ``Store`` on ``tmp_path``,
``asyncio_mode = "auto"`` (set repo-wide) so no ``@pytest.mark.asyncio`` is
needed.
"""

from __future__ import annotations

import subprocess

import pytest

from no_human.agent.backend import local_run_without_subscription
from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.blockers import BlockerCategory
from no_human.config import load_config
from no_human.core.bounds import QuotaExhausted
from no_human.core.db import Store
from no_human.core.infra_breaker import infra_breaker
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

# Replayed verbatim from the 2026-09-01 incident (litellm over ollama).
_LOCAL_DEATH_TEXT = (
    "litellm.InternalServerError: OllamaException - "
    "{'error': 'llama3 does not support thinking'}"
)


@pytest.fixture(autouse=True)
def _clean_infra_breaker_singleton():
    """The breaker is a process-wide singleton; reset it around every test in
    this file so one test's infra failures can never leak into the next
    test's assertions (same rationale as `test_infra_not_work.py`)."""
    infra_breaker().reset()
    yield
    infra_breaker().reset()


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@e.com")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (work / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _config(tmp_path, *, backend: str = "claude"):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("worker", {})["backend"] = backend
    if backend == "local":
        cfg.data.setdefault("llm", {})["local_base_url"] = "http://127.0.0.1:11434"
    return cfg


class _ScriptedBackend:
    """Stands in for ClaudeBackend: always returns the same scripted
    `AgentResult`, whatever attempt asks for it. Optionally streams `events`
    through `on_event` before returning, so a test can exercise the
    agent_text-event fallback path."""

    def __init__(self, result: AgentResult, events: list[AgentEvent] | None = None):
        self._result = result
        self._events = events or []
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        if on_event is not None:
            for ev in self._events:
                on_event(ev)
        return self._result


def _local_death_result(**overrides) -> AgentResult:
    fields = dict(
        final_text=_LOCAL_DEATH_TEXT, num_turns=1, is_error=True, tokens_used=0,
        session_id=None, stop_reason=None, cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    fields.update(overrides)
    return AgentResult(**fields)


async def _run_one_attempt(store, bare_repo, tmp_path, result, *, backend="claude",
                           events=None):
    cfg = _config(tmp_path, backend=backend)
    fake = _ScriptedBackend(result, events=events)
    orch = Orchestrator(store, cfg.data, fake, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, fake, task, repo


# --------------------------------------------------------------------------- #
# End-to-end: `run_task` replay, local vs. claude                             #
# --------------------------------------------------------------------------- #


async def test_local_backend_zero_token_death_is_infra_not_quota(store, bare_repo, tmp_path):
    """The measured incident shape, driven through the real `run_task` loop.
    RED before the fix: `local_run_without_subscription` does not exist and
    the zero-token death routes into `_park_quota`, naming a subscription
    reset that means nothing in local mode."""
    cfg = _config(tmp_path, backend="local")
    backend = _ScriptedBackend(_local_death_result())
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)

    outcome = await orch.run_task(task)
    parked = await store.get_task(task.id)

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status is not TaskStatus.PAUSED_QUOTA, (
        f"a local infra death waited on a subscription reset that does not "
        f"exist in local mode: {outcome.status} {outcome.detail}")
    assert parked.blocker is not None
    assert parked.blocker["category"] == BlockerCategory.TRANSIENT_INFRA.value
    assert parked.blocker["wake_condition"] == "after:30m"
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0, "a dead local dispatch charged a lifetime attempt"


async def test_claude_backend_zero_token_death_still_pauses_quota(store, bare_repo, tmp_path):
    """Mirror: the identical zero-token death shape on `backend=claude` (a
    real subscription in play) must still route to `paused_quota` exactly as
    today — this fix must change routing for the local case ONLY."""
    cfg = _config(tmp_path, backend="claude")
    backend = _ScriptedBackend(_local_death_result())
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)

    outcome = await orch.run_task(task)
    parked = await store.get_task(task.id)

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status is TaskStatus.PAUSED_QUOTA, (
        f"claude-backend quota routing changed: {outcome.status} {outcome.detail}")
    assert parked.blocker["wake_condition"] == "quota_refreshed"


# --------------------------------------------------------------------------- #
# The child's error text reaches the blocker                                  #
# --------------------------------------------------------------------------- #


async def test_the_local_backends_own_error_text_reaches_the_blocker(store, bare_repo, tmp_path):
    """Criterion: the actual stderr/500 body must be visible in the park
    reason, not just in `agent_text` events (as it was before this fix)."""
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _local_death_result(), backend="local")

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls
    assert outcome.status is not TaskStatus.PAUSED_QUOTA
    fresh = await store.get_task(task.id)
    assert "does not support thinking" in fresh.blocker["evidence"], fresh.blocker
    assert "OllamaException" in fresh.blocker["evidence"], fresh.blocker


async def test_error_text_from_agent_text_events_when_final_text_is_empty(store, bare_repo, tmp_path):
    """The measured shape: the proxy's error prose streamed through an
    `agent_text` event, but the result's own `final_text` was empty. The
    blocker must still surface it via `_last_coder_error_text`."""
    death = _local_death_result(final_text="")
    events = [AgentEvent(kind="text", text=_LOCAL_DEATH_TEXT)]
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, death, backend="local", events=events)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls
    assert outcome.status is not TaskStatus.PAUSED_QUOTA
    fresh = await store.get_task(task.id)
    assert "does not support thinking" in fresh.blocker["evidence"], fresh.blocker


async def test_a_local_infra_park_still_spares_the_lifetime_attempt(store, bare_repo, tmp_path):
    """The local-infra route must keep the same accounting guarantee the
    quota route has always had: a dead dispatch never burns a lifetime
    attempt."""
    orch, backend, task, repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _local_death_result(), backend="local")

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert outcome.status is not TaskStatus.PAUSED_QUOTA
    used_attempts, _, _ = await store.lifetime_usage_by_class(task.id)
    assert used_attempts == 0
    assert await store.count_attempts(task.id) == 1


# --------------------------------------------------------------------------- #
# `local_run_without_subscription` — the predicate itself                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend_name,expected", [
    ("local", True),
    ("claude", False),
    ("codex", False),
    ("api", False),
])
def test_local_run_without_subscription_matches_only_local(tmp_path, backend_name, expected):
    cfg = _config(tmp_path, backend="claude")  # base; overridden below
    cfg.data.setdefault("worker", {})["backend"] = backend_name
    assert local_run_without_subscription(cfg.data) is expected


def test_predicate_is_false_when_the_child_env_carries_a_token(tmp_path, monkeypatch):
    """If `_local_child_env` ever stops blanking the OAuth token, the
    predicate must fail BACK to today's (quota) behaviour, not keep firing
    on a stale restatement of `worker.backend == "local"`."""
    import no_human.agent.backend as backend_mod

    monkeypatch.setattr(
        backend_mod, "_local_child_env",
        lambda llm_cfg: {"ANTHROPIC_BASE_URL": "x", "ANTHROPIC_API_KEY": "y",
                          "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-real-token"})
    cfg = _config(tmp_path, backend="local")
    assert local_run_without_subscription(cfg.data) is False


# --------------------------------------------------------------------------- #
# `_park_quota` defence-in-depth guard                                        #
# --------------------------------------------------------------------------- #


async def test_park_quota_refuses_to_pause_a_local_infra_exception(store, bare_repo, tmp_path):
    """Any OTHER raise site (planner, repro corrective round) that raises an
    infra `QuotaExhausted` against a local-without-subscription config must
    still not land in `paused_quota` — the guard belongs to `_park_quota`
    itself, not just the one coder call site."""
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _local_death_result(), backend="local")

    outcome = await orch._park_quota(task, QuotaExhausted("dead dispatch", infra=True))

    assert outcome.status is not TaskStatus.PAUSED_QUOTA


async def test_park_quota_still_pauses_a_claude_infra_exception(store, bare_repo, tmp_path):
    """Mirror of the guard test: the same call, against a `claude` config,
    must still take the ordinary quota park — the guard is local-only."""
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _local_death_result(), backend="claude")

    outcome = await orch._park_quota(task, QuotaExhausted("dead dispatch", infra=True))

    assert outcome.status is TaskStatus.PAUSED_QUOTA


# --------------------------------------------------------------------------- #
# Per-task `--backend` override (task.config["backend"])                     #
#                                                                             #
# `core.runtime.task_backend_override` / `Orchestrator._task_backend` honour  #
# a PER-TASK backend choice ahead of the global `worker.backend` — the exact  #
# resolution the coder session was actually built with. The classifier must   #
# follow that same resolution, not just the global config, in both           #
# directions: a `local` task on a `claude`-global install, and a `claude`     #
# task on a `local`-global install.                                          #
# --------------------------------------------------------------------------- #


async def test_per_task_local_override_on_claude_global_is_infra_not_quota(
    store, bare_repo, tmp_path,
):
    """`nh task add --backend local` on a config whose GLOBAL `worker.backend`
    is `claude` must still route a zero-token death to infra, never
    `paused_quota` — the exact gap the first review round caught: both guard
    sites resolved only `self.config` (the global backend), missing this
    per-task override entirely."""
    cfg = _config(tmp_path, backend="claude")  # global says claude
    backend = _ScriptedBackend(_local_death_result())
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    task.config = {"backend": "local"}  # per-task override says local
    await store.create_task(task)

    outcome = await orch.run_task(task)
    parked = await store.get_task(task.id)

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status is not TaskStatus.PAUSED_QUOTA, (
        f"a per-task local override still waited on a subscription reset "
        f"that does not exist for this task: {outcome.status} {outcome.detail}")
    assert parked.blocker is not None
    assert parked.blocker["category"] == BlockerCategory.TRANSIENT_INFRA.value
    assert parked.blocker["wake_condition"] == "after:30m"


async def test_per_task_claude_override_on_local_global_still_pauses_quota(
    store, bare_repo, tmp_path,
):
    """Mirror: `nh task add --backend claude` on a config whose GLOBAL
    `worker.backend` is `local` must still take the ordinary quota park — a
    real subscription IS in play for this task, so the predicate must not
    fire off the global config alone."""
    cfg = _config(tmp_path, backend="local")  # global says local
    backend = _ScriptedBackend(_local_death_result())
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    task.config = {"backend": "claude"}  # per-task override says claude
    await store.create_task(task)

    outcome = await orch.run_task(task)
    parked = await store.get_task(task.id)

    assert backend.calls, "the backend never ran — the test proves nothing"
    assert outcome.status is TaskStatus.PAUSED_QUOTA, (
        f"a per-task claude override was misrouted to infra by the global "
        f"local config: {outcome.status} {outcome.detail}")
    assert parked.blocker["wake_condition"] == "quota_refreshed"


async def test_park_quota_guard_honours_a_per_task_local_override(store, bare_repo, tmp_path):
    """The `_park_quota` defence-in-depth guard itself (not just the coder
    call site) must resolve the per-task override too — this is what the
    planner (`_drive_watched`) and the repro corrective round rely on."""
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _local_death_result(), backend="claude")
    task.config = {"backend": "local"}

    outcome = await orch._park_quota(task, QuotaExhausted("dead dispatch", infra=True))

    assert outcome.status is not TaskStatus.PAUSED_QUOTA


async def test_park_quota_guard_honours_a_per_task_claude_override(store, bare_repo, tmp_path):
    """Mirror: the guard must not fire off a `local`-global config when the
    TASK itself overrides to `claude`."""
    orch, _backend, task, _repo = await _run_one_attempt(
        store, bare_repo, tmp_path, _local_death_result(), backend="local")
    task.config = {"backend": "claude"}

    outcome = await orch._park_quota(task, QuotaExhausted("dead dispatch", infra=True))

    assert outcome.status is TaskStatus.PAUSED_QUOTA


def test_local_run_without_subscription_backend_name_overrides_config(tmp_path):
    """The predicate's `backend_name` kwarg — not just `config` — decides the
    outcome, proving callers with a per-task backend in scope actually
    change the result rather than the parameter being accepted and ignored."""
    claude_cfg = _config(tmp_path, backend="claude")
    local_cfg = _config(tmp_path, backend="local")

    assert local_run_without_subscription(claude_cfg.data, backend_name="local") is True
    assert local_run_without_subscription(local_cfg.data, backend_name="claude") is False
