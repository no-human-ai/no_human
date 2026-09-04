"""The "a human send-back onto an exhausted budget silently FAILS the task"
defect (2026-08-22, `.no_human/PLAN.md`): a send-back delivered while a
loop-head gate (lifetime budget, attempt startup floor — before an attempt
row exists) refuses to start a round used to look identical to organic
exhaustion. The reviewer's feedback was recorded (``send_back_feedback``) and
the task moved, but nothing ever said the send-back itself was refused.

RED on pre-fix code: none of `Blocker.send_back_refused`, `_refuse_round`,
`blockers.send_back` existed before this fix — every assertion below reads a
field, event kind, or module that pre-fix code cannot produce. See the final
report for the exact RED-first argument (no `git stash` is used, per the
outer harness rule against git commands in this session).
"""
from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from no_human.blockers import Blocker, BlockerCategory, record_pending_send_back
from no_human.cli.commands import cli
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Fixtures / helpers — copied from tests/test_e2e_orchestrator.py and
# tests/test_lifetime_budget.py conventions (fixtures do not cross test
# files without a conftest, so these are deliberately re-stated, not
# imported).                                                                  #
# --------------------------------------------------------------------------- #

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


class _MustNotRunBackend:
    """Proves a "don't start" gate for real: asserts if the loop ever reaches
    the backend, the way `test_e2e_orchestrator.py`'s twin does."""

    def __init__(self):
        self.calls = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls.append(prompt)
        raise AssertionError(
            "the backend ran — a loop-head gate should have refused the "
            "round before any attempt started")


class FakeBackend:
    """Stands in for ClaudeBackend: applies a scripted file mutation."""

    def __init__(self, mutate):
        self.mutate = mutate

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        from no_human.agent.claude_backend import AgentEvent, AgentResult
        if on_event:
            on_event(AgentEvent("tool_use", tool_name="Edit",
                                tool_input={"file_path": "calc.py"}))
        self.mutate(cwd)
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=100, session_id="s", stop_reason="end_turn")


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


async def _spend(store, task_id, attempts, tokens_each):
    """Copied from test_lifetime_budget.py: burns `attempts` attempt rows,
    each with `tokens_each` raw tokens split fresh/cache-read, so the
    lifetime-usage gate sees real spend without a live model call."""
    for n in range(1, attempts + 1):
        aid = await store.create_attempt(task_id, n)
        await store.update_attempt(
            aid, tokens_used=tokens_each // 2,
            cache_read_tokens=tokens_each - tokens_each // 2)


def _events_of_kind(events, kind):
    return [e for e in events if e.get("kind") == kind]


# --------------------------------------------------------------------------- #
# AC1 — lifetime-token ceiling                                                #
# --------------------------------------------------------------------------- #

async def test_send_back_onto_an_exhausted_token_budget_names_the_sendback_and_the_remedy(
    bare_repo, tmp_path, store,
):
    cfg = _config(tmp_path)
    backend = _MustNotRunBackend()
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("fix the thing", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["it works"]
    t.config = {"lifetime_tokens": 100, "budget_unit": "weighted"}
    await store.create_task(t)
    # Already over the (tiny) cap before the send-back arrives.
    await _spend(store, t.id, attempts=1, tokens_each=10_000)
    attempts_before = await store.list_attempts(t.id)

    message = "x" * 4495
    await record_pending_send_back(
        store, t, source="send_back", message=message, actor="operator:api")

    outcome = await orch.run_task(t)

    assert backend.calls == [], "the backend ran despite an exhausted budget"
    assert await store.list_attempts(t.id) == attempts_before, (
        "a send-back onto an exhausted budget must not create a new attempt row")

    fresh = await store.find_task(t.id)
    blocker = fresh.blocker or {}
    assert blocker.get("category") == "BUDGET_EXHAUSTED", blocker
    rc = blocker.get("root_cause_hypothesis", "")
    assert "your send-back could not start a round" in rc, rc
    assert "lifetime budget" in rc, rc
    assert "nh task config" in rc, rc
    sbr = blocker.get("send_back_refused") or {}
    assert sbr.get("source") == "send_back", sbr

    refusals = _events_of_kind(events, "send_back_refused")
    assert len(refusals) == 1, events
    ev = refusals[0]
    assert ev["gate"] == "lifetime budget", ev
    assert "nh task config" in ev["remedy"], ev
    assert ev["feedback_chars"] == 4495, ev
    assert ev["task_id"] == t.id, ev
    assert "pr_url" in ev, ev


# --------------------------------------------------------------------------- #
# AC2 — attempt-count ceiling                                                 #
# --------------------------------------------------------------------------- #

async def test_send_back_at_the_attempt_ceiling_says_so_too(
    bare_repo, tmp_path, store,
):
    cfg = _config(tmp_path)
    backend = _MustNotRunBackend()
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("fix the thing", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["it works"]
    await store.create_task(t)
    # Default lifetime_attempts cap is 9 (Bounds()) — negligible tokens, so
    # only the attempt-count axis is over.
    await _spend(store, t.id, attempts=9, tokens_each=100)
    attempts_before = await store.list_attempts(t.id)

    await record_pending_send_back(
        store, t, source="send_back", message="please fix the edge case",
        actor="operator:api")

    outcome = await orch.run_task(t)

    assert backend.calls == []
    assert await store.list_attempts(t.id) == attempts_before

    fresh = await store.find_task(t.id)
    blocker = fresh.blocker or {}
    assert blocker.get("category") == "BUDGET_EXHAUSTED", blocker
    rc = blocker.get("root_cause_hypothesis", "")
    assert "your send-back could not start a round" in rc, rc
    assert "attempts 9/9" in rc, rc
    sbr = blocker.get("send_back_refused") or {}
    assert sbr.get("source") == "send_back", sbr

    refusals = _events_of_kind(events, "send_back_refused")
    assert len(refusals) == 1, events
    ev = refusals[0]
    assert ev["gate"] == "lifetime budget", ev
    assert "lifetime_attempts=" in ev["remedy"], ev


# --------------------------------------------------------------------------- #
# AC2b — startup-floor gate (proves the funnel is gate-agnostic)              #
# --------------------------------------------------------------------------- #

async def test_the_startup_floor_refusal_also_names_the_sendback(
    bare_repo, tmp_path, store,
):
    cfg = _config(tmp_path)
    backend = _MustNotRunBackend()
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns product"]
    t.context = {"eval_result": {"verdict": "accept"}}
    # Under the lifetime cap (nothing spent yet) but the remaining budget is
    # below the startup floor (default 250,000 weighted tokens).
    t.config = {"lifetime_tokens": 100, "budget_unit": "weighted"}
    await store.create_task(t)

    await record_pending_send_back(
        store, t, source="reject", message="one more pass please", actor="cli")

    outcome = await orch.run_task(t)

    assert backend.calls == []
    assert await store.list_attempts(t.id) == []

    fresh = await store.find_task(t.id)
    blocker = fresh.blocker or {}
    assert blocker.get("category") == "BUDGET_EXHAUSTED", blocker
    rc = blocker.get("root_cause_hypothesis", "")
    assert "your send-back could not start a round" in rc, rc
    assert "attempt startup floor" in rc, rc
    assert "floor" in rc, rc
    sbr = blocker.get("send_back_refused") or {}
    assert sbr.get("source") == "reject", sbr

    refusals = _events_of_kind(events, "send_back_refused")
    assert len(refusals) == 1, events
    assert refusals[0]["gate"] == "attempt startup floor", refusals[0]


# --------------------------------------------------------------------------- #
# AC3 — negative control: a send-back that CAN start a round is unchanged     #
# --------------------------------------------------------------------------- #

async def test_a_sendback_that_can_start_a_round_is_unchanged(
    bare_repo, tmp_path, store,
):
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    events = []
    orch = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)
    # Plenty of headroom — both loop-head gates pass.
    await record_pending_send_back(
        store, t, source="send_back", message="looks close, please add mul()",
        actor="operator:api")

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    attempts = await store.list_attempts(t.id)
    assert len(attempts) == 1, "a send-back with headroom must still start a round"
    assert not _events_of_kind(events, "send_back_refused"), (
        "no send_back_refused event when the round actually started")
    fresh = await store.find_task(t.id)
    assert fresh.blocker is None
    assert (fresh.context or {}).get("pending_send_back") is None, (
        "attempt_start must clear the pending marker once a round actually starts")


# --------------------------------------------------------------------------- #
# Producer coverage — all three send-back paths record the marker             #
# --------------------------------------------------------------------------- #

async def _seed_task(store, *, status=TaskStatus.AWAITING_APPROVAL):
    t = Task.new("Fix thing", repo_path="/tmp/repo")
    t.acceptance_criteria = ["Should work"]
    await store.create_task(t)
    await store.set_status(t, status, validate=False)
    return t


async def test_api_send_back_records_the_pending_marker(store, tmp_path):
    from no_human.api.app import app

    t = await _seed_task(store)
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        r = await client.post(
            f"/api/tasks/{t.id}/send-back",
            json={"message": "handle the empty-input edge case"},
        )
    assert r.status_code == 200, r.text

    fresh = await store.find_task(t.id)
    pending = (fresh.context or {}).get("pending_send_back") or {}
    assert pending.get("source") == "send_back", pending


def _make_cli_runner(db_path, monkeypatch):
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = db_path
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(
        cmd_mod, "_probe_pool",
        lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED))
    return CliRunner()


def test_cli_reject_records_the_pending_marker(tmp_path, monkeypatch):
    """`reject` calls `asyncio.run()` internally (see
    `tests/test_cli_commands.py`'s module docstring), so — like every test
    there — this test itself must be SYNCHRONOUS: an async test function
    would already be inside a running event loop and `asyncio.run()` would
    raise. Setup/verification each open their own fresh connection in their
    own `asyncio.run()`, same pattern as `test_cli_commands.py::_seed_task`.
    """
    import asyncio

    db_path = tmp_path / "nh.db"

    async def _seed():
        async with Store(db_path) as s:
            t = Task.new("Fix thing", repo_path="/tmp/repo")
            t.acceptance_criteria = ["Should work"]
            await s.create_task(t)
            await s.set_status(t, TaskStatus.AWAITING_APPROVAL, validate=False)
            return t.id

    task_id = asyncio.run(_seed())
    runner = _make_cli_runner(db_path, monkeypatch)

    result = runner.invoke(cli, ["reject", task_id[:8], "--reason", "please fix X"])
    assert result.exit_code == 0, result.output

    async def _read():
        async with Store(db_path) as s:
            return await s.find_task(task_id)

    fresh = asyncio.run(_read())
    pending = (fresh.context or {}).get("pending_send_back") or {}
    assert pending.get("source") == "reject", pending
    assert pending.get("actor") == "cli", pending


async def test_pr_comment_feedback_records_the_pending_marker(store):
    from no_human.blockers.wake import WakeWatcher

    t = Task.new("conflict", repo_path="/tmp/x")
    t.context = {}
    await store.create_task(t)
    w = WakeWatcher(store, {})

    await w._append_comments_as_feedback(
        t, ["please handle the empty-input case"],
        pr_ref="https://example.com/pr/9")

    fresh = await store.find_task(t.id)
    pending = (fresh.context or {}).get("pending_send_back") or {}
    assert pending.get("source") == "pr_comment", pending
    assert pending.get("pr_comment_ref") == "https://example.com/pr/9", pending


# --------------------------------------------------------------------------- #
# Blocker round-trip                                                          #
# --------------------------------------------------------------------------- #

def test_blocker_round_trips_the_sendback_refusal_marker():
    b = Blocker(
        category=BlockerCategory.BUDGET_EXHAUSTED,
        root_cause_hypothesis="your send-back could not start a round",
        send_back_refused={
            "task_id": "abc123", "gate": "lifetime budget",
            "source": "send_back", "remedy": "nh task config ...",
        },
    )
    round_tripped = Blocker.from_dict(b.to_dict())
    assert round_tripped.send_back_refused == b.send_back_refused


# --------------------------------------------------------------------------- #
# Repeat-refusal suppression (2026-08-26 fix, `.no_human/PLAN.md`): a refused  #
# send-back's pending marker used to never get stamped, so every later resume #
# of the same exhausted budget emitted a fresh `send_back_refused` event with #
# a new `refused_at` — a PR-side consumer de-duping on the event stream would #
# post a duplicate comment per wake.                                          #
# --------------------------------------------------------------------------- #

async def test_a_second_refused_resume_does_not_emit_a_second_refusal_event(
    bare_repo, tmp_path, store,
):
    cfg = _config(tmp_path)
    backend = _MustNotRunBackend()
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("fix the thing", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["it works"]
    t.config = {"lifetime_tokens": 100, "budget_unit": "weighted"}
    await store.create_task(t)
    await _spend(store, t.id, attempts=1, tokens_each=10_000)

    await record_pending_send_back(
        store, t, source="send_back", message="please fix it",
        actor="operator:api")

    await orch.run_task(t)
    first = await store.find_task(t.id)
    sbr1 = (first.blocker or {}).get("send_back_refused") or {}
    assert sbr1.get("refused_at"), sbr1

    # Second resume, no new human action — re-read the task between runs, as
    # a real resume would.
    await orch.run_task(first)
    second = await store.find_task(t.id)

    assert backend.calls == [], "a loop-head gate should have refused both resumes"
    refusals = _events_of_kind(events, "send_back_refused")
    assert len(refusals) == 1, events

    pending = (second.context or {}).get("pending_send_back") or {}
    assert pending.get("refused_at"), pending
    assert pending.get("refused_gate") == "lifetime budget", pending

    blocker = second.blocker or {}
    rc = blocker.get("root_cause_hypothesis", "")
    assert "your send-back could not start a round" in rc, rc
    assert "nh task config" in rc, rc
    assert "remedy: waiting on a human" in rc, rc

    sbr2 = blocker.get("send_back_refused") or {}
    assert sbr2.get("refused_at") == sbr1.get("refused_at"), (sbr1, sbr2)


async def test_a_new_send_back_after_a_refusal_emits_again(
    bare_repo, tmp_path, store,
):
    cfg = _config(tmp_path)
    backend = _MustNotRunBackend()
    events = []
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("fix the thing", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["it works"]
    t.config = {"lifetime_tokens": 100, "budget_unit": "weighted"}
    await store.create_task(t)
    await _spend(store, t.id, attempts=1, tokens_each=10_000)

    await record_pending_send_back(
        store, t, source="send_back", message="please fix it",
        actor="operator:api")

    await orch.run_task(t)
    first = await store.find_task(t.id)
    refusals = _events_of_kind(events, "send_back_refused")
    assert len(refusals) == 1, events

    new_message = "second try, please also handle negatives"
    await record_pending_send_back(
        store, first, source="send_back", message=new_message,
        actor="operator:cli")

    second = await store.find_task(t.id)
    await orch.run_task(second)
    third = await store.find_task(t.id)

    assert backend.calls == [], "still no round should have started"
    refusals = _events_of_kind(events, "send_back_refused")
    assert len(refusals) == 2, events
    assert refusals[0]["refused_at"] != refusals[1]["refused_at"], events
    assert refusals[1]["feedback_chars"] == len(new_message), refusals[1]
    assert refusals[1]["actor"] == "operator:cli", refusals[1]
    assert new_message in refusals[1]["excerpt"], refusals[1]

    pending = (third.context or {}).get("pending_send_back") or {}
    assert pending.get("refused_at") == refusals[1]["refused_at"], pending
    assert pending.get("refused_gate") == "lifetime budget", pending


async def test_raising_the_budget_after_a_refusal_clears_the_stamped_marker(
    bare_repo, tmp_path, store,
):
    def mutate(cwd):
        (cwd / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "def mul(a, b):\n    return a * b\n"
        )
        (cwd / "test_calc.py").write_text(
            "from calc import add, mul\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n"
        )

    cfg = _config(tmp_path)
    events = []
    backend = _MustNotRunBackend()
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=events.append)
    t = Task.new("add mul()", repo_path=str(bare_repo), kind="feature")
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    t.config = {"lifetime_tokens": 100, "budget_unit": "weighted"}
    await store.create_task(t)
    await _spend(store, t.id, attempts=1, tokens_each=10_000)

    await record_pending_send_back(
        store, t, source="send_back", message="please add mul()",
        actor="operator:api")

    await orch.run_task(t)
    assert backend.calls == []
    refusals = _events_of_kind(events, "send_back_refused")
    assert len(refusals) == 1, events

    fresh = await store.find_task(t.id)
    assert (fresh.context or {}).get("pending_send_back", {}).get("refused_at"), (
        "the marker must be stamped by the refused round above")

    # Give headroom, then force the status back to PENDING the same way
    # `test_lifetime_budget.py::test_a_review_pass_freeze_is_exempt_from_the_startup_floor`
    # forces a status for a test (`set_status(..., validate=False)`):
    # `update_task` never writes the status column (db.py:1559-1576), and a
    # plain FAILED row (no `cancel_reason`) stays writable per `set_status`'s
    # own CAS-guard docstring, so this is the same bypass a human's `nh task
    # retry` uses, done directly for the test.
    fresh.config = {"lifetime_tokens": 10_000_000, "budget_unit": "weighted"}
    await store.update_task(fresh)
    await store.set_status(fresh, TaskStatus.PENDING, validate=False)
    fresh = await store.find_task(t.id)

    orch2 = Orchestrator(store, cfg.data, FakeBackend(mutate), SlackNotifier(None),
                         event_sink=events.append)
    outcome = await orch2.run_task(fresh)

    assert outcome.status is TaskStatus.AWAITING_APPROVAL, outcome
    final = await store.find_task(t.id)
    assert (final.context or {}).get("pending_send_back") is None, (
        "attempt_start must clear the pending marker once a round actually starts")

    refusals = _events_of_kind(events, "send_back_refused")
    assert len(refusals) == 1, events


async def test_mark_send_back_refused_is_fail_open_and_owns_only_its_two_fields(
    store,
):
    from no_human.blockers import mark_send_back_refused

    class _ExplodingStore:
        async def merge_context(self, task_id, patch):
            raise RuntimeError("boom")

    class _StubTask:
        id = "deadbeef"
        context: dict = {}

    result = await mark_send_back_refused(
        _ExplodingStore(), _StubTask(), gate="lifetime budget")
    assert result is None

    # Real-store case: the stamp is a MERGE, not a rewrite — the existing
    # at/source/actor/excerpt/chars/pr_comment_ref fields on the marker
    # survive byte-identical.
    t = Task.new("fix the thing", repo_path="/tmp/repo")
    await store.create_task(t)
    entry = await record_pending_send_back(
        store, t, source="send_back", message="please fix it",
        actor="operator:api", at="2026-01-01T00:00:00+00:00")
    assert entry is not None

    stamped = await mark_send_back_refused(
        store, t, gate="lifetime budget", at="2026-01-02T00:00:00+00:00")
    assert stamped == {
        "refused_at": "2026-01-02T00:00:00+00:00",
        "refused_gate": "lifetime budget",
    }

    fresh = await store.find_task(t.id)
    pending = (fresh.context or {}).get("pending_send_back") or {}
    assert pending.get("at") == entry["at"]
    assert pending.get("source") == entry["source"]
    assert pending.get("actor") == entry["actor"]
    assert pending.get("excerpt") == entry["excerpt"]
    assert pending.get("chars") == entry["chars"]
    assert pending.get("pr_comment_ref") == entry["pr_comment_ref"]
    assert pending.get("refused_at") == "2026-01-02T00:00:00+00:00"
    assert pending.get("refused_gate") == "lifetime budget"
