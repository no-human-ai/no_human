"""Act on a stored intake-eval verdict at dispatch: grill-sourced tasks are
annotated but never enriched — regression coverage for the fix.

Board/API bare creates ran the intake evaluator at dispatch and then acted on
its verdict (ENRICH/CLARIFY/DECOMPOSE). Grill/wizard-sourced tasks arrive with
``context['eval_result']`` already populated, so they hit the dispatch guard's
``if not eval_result`` branch and skipped straight past ``_act_on_eval`` —
annotated, never enriched. This file covers the hoisted ``elif`` path
(``_act_on_stored_eval``), its idempotency marker, the merge-not-clobber write
path inside ``_act_on_eval``, and ``EvalResult.from_dict``.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from no_human.agent.claude_backend import AgentEvent, AgentResult
from no_human.api.app import app
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.intake import evaluator as ev
from no_human.intake import grill as grill_mod
from no_human.intake.evaluator import EvalResult, EvalVerdict
from no_human.intake.grill import GrillResult
from no_human.notify.slack import SlackNotifier

from tests.test_e2e_orchestrator import bare_repo  # noqa: F401 — shared fixture


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


@pytest.fixture
async def client(store, tmp_path):
    """Real ASGI app, real HTTP surface — mirrors test_api_task_follows.py's
    fixture. Used by the grill->create_task end-to-end test below, which
    needs the actual /api/grill/stream and /api/tasks routes, not a
    hand-built Task."""
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


# --------------------------------------------------------------------------- #
# EvalResult.from_dict                                                        #
# --------------------------------------------------------------------------- #

def test_from_dict_round_trips_as_dict():
    x = EvalResult(
        verdict=EvalVerdict.ENRICH,
        dimensions={"clear_objective": True, "testable_criteria": False},
        enriched_criteria=["a", "b"],
        rationale="needs sharper criteria",
        tokens_used=123,
        cache_read_tokens=10,
        cache_creation_tokens=5,
    )
    got = EvalResult.from_dict(x.as_dict())
    assert got.verdict == x.verdict
    assert got.dimensions == x.dimensions
    assert got.enriched_criteria == x.enriched_criteria
    assert got.rationale == x.rationale
    assert got.tokens_used == x.tokens_used
    assert got.cache_read_tokens == x.cache_read_tokens
    assert got.cache_creation_tokens == x.cache_creation_tokens


def test_from_dict_thin_dict_does_not_raise():
    got = EvalResult.from_dict({"verdict": "accept"})
    assert got.verdict == EvalVerdict.ACCEPT
    assert got.dimensions == {}
    assert got.enriched_criteria is None
    assert got.rationale == ""
    assert got.tokens_used == 0


def test_from_dict_unknown_verdict_falls_back_to_accept():
    got = EvalResult.from_dict({"verdict": "not-a-real-verdict"})
    assert got.verdict == EvalVerdict.ACCEPT


def test_from_dict_empty_dict_does_not_raise():
    got = EvalResult.from_dict({})
    assert got.verdict == EvalVerdict.ACCEPT
    assert got.dimensions == {}


# --------------------------------------------------------------------------- #
# AC 1: stored ENRICH verdict is adopted                                      #
# --------------------------------------------------------------------------- #

async def test_stored_enrich_verdict_is_adopted(store, tmp_path):
    t = Task.new("do a thing", repo_path="/r")
    t.acceptance_criteria = ["stated one"]
    t.context = {
        "eval_result": {
            "verdict": "enrich",
            "enriched_criteria": ["sharp A", "sharp B"],
            "dimensions": {},
        }
    }
    await store.create_task(t)
    orch = _orch(store, tmp_path)

    await orch._act_on_stored_eval(t)

    got = await store.get_task(t.id)
    assert got.acceptance_criteria == ["sharp A", "sharp B"]
    assert got.context["original_criteria"] == ["stated one"]
    assert got.context["eval_acted"] is True


async def test_grilled_task_reaches_planning_enriched(bare_repo, tmp_path, store, monkeypatch):
    """End-to-end: proves the wiring in ``_drive``, not just the method."""

    async def _fail_if_called(*a, **k):  # pragma: no cover
        raise AssertionError("evaluate_spec must not run for a grilled task")

    monkeypatch.setattr(ev, "evaluate_spec", _fail_if_called)

    class SimpleBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            (cwd / "calc.py").write_text(
                "def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add, mul\n\n"
                "def test_add():\n    assert add(1, 2) == 3\n\n"
                "def test_mul():\n    assert mul(2, 3) == 6\n")
            return AgentResult(final_text="done", num_turns=2, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    orch = Orchestrator(store, cfg.data, SimpleBackend(), SlackNotifier(None))

    t = Task.new("grilled task", repo_path=str(bare_repo))
    t.acceptance_criteria = ["stated one"]
    t.context = {
        "eval_result": {
            "verdict": "enrich",
            "enriched_criteria": ["sharp A", "sharp B"],
            "dimensions": {},
        }
    }
    await store.create_task(t)

    await orch.run_task(t)

    got = await store.get_task(t.id)
    assert got.acceptance_criteria == ["sharp A", "sharp B"]
    assert got.context["original_criteria"] == ["stated one"]


async def test_grill_http_flow_creates_the_eval_result_precondition_end_to_end(
        client, store, bare_repo, tmp_path, monkeypatch):
    """The precondition the two tests above hand-set (``t.context['eval_result']``)
    must actually be reachable from production traffic, or the stored-verdict
    path is dead for the exact tasks the ticket targets (grill/wizard). This
    drives the REAL surface: POST /api/grill/stream (the SSE the composer
    calls) for the eval_verdict frame, then POST /api/tasks (the web client's
    createTask, now forwarding eval_result) — no hand-set context anywhere —
    and only then runs the task and checks it came out enriched.
    """

    async def _fake_grill_step(title, description, repo_path, qa_history,
                                backend, *, on_event=None, max_rounds=None):
        return GrillResult(
            title=title, description=description or "",
            acceptance_criteria=["stated one"],
        )
    monkeypatch.setattr(grill_mod, "grill_step", _fake_grill_step)

    async def _fake_evaluate_spec(title, description, criteria, *, model=None):
        return EvalResult(verdict=EvalVerdict.ENRICH,
                          enriched_criteria=["sharp A", "sharp B"])
    monkeypatch.setattr(ev, "evaluate_spec", _fake_evaluate_spec)

    frames = []
    async with client.stream(
        "POST", "/api/grill/stream",
        json={"title": "grilled task", "repo_path": str(bare_repo)},
    ) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))

    eval_frames = [f for f in frames if f.get("kind") == "eval_verdict"]
    assert len(eval_frames) == 1, frames
    # Exactly what App.jsx's _evalResultField strips off the SSE envelope
    # (kind/source) before handing the rest to createTask as eval_result.
    eval_result_payload = {k: v for k, v in eval_frames[0].items()
                           if k not in ("kind", "source")}
    assert eval_result_payload["verdict"] == "enrich"

    r = await client.post("/api/tasks", json={
        "title": "grilled task",
        "repo_path": str(bare_repo),
        "acceptance_criteria": ["stated one"],
        "eval_result": eval_result_payload,
    })
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    # The precondition, produced entirely through HTTP — this is the fact
    # under test, not an assumption baked into a hand-built Task.
    created = await store.get_task(task_id)
    assert created.context["eval_result"]["verdict"] == "enrich"

    class SimpleBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                      on_event=None, supervisor_hook=None, **kwargs):
            (cwd / "calc.py").write_text("def add(a, b):\n    return a + b\n")
            (cwd / "test_calc.py").write_text(
                "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
            return AgentResult(final_text="done", num_turns=1, is_error=False,
                               tokens_used=100, session_id="s", stop_reason="end_turn")

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("blockers", {})["challenge"] = False
    orch = Orchestrator(store, cfg.data, SimpleBackend(), SlackNotifier(None))

    task = await store.get_task(task_id)
    await orch.run_task(task)

    got = await store.get_task(task_id)
    assert got.acceptance_criteria == ["sharp A", "sharp B"]
    assert got.context["original_criteria"] == ["stated one"]


# --------------------------------------------------------------------------- #
# AC 2: idempotency                                                           #
# --------------------------------------------------------------------------- #

async def test_acting_on_stored_eval_is_idempotent(store, tmp_path, monkeypatch):
    evaluate_calls = []
    resolve_calls = []

    async def _fail_evaluate(*a, **k):
        evaluate_calls.append(True)
        raise AssertionError("evaluate_spec must not run on the stored-eval path")

    async def _fake_resolve(title, description, criteria, *, backend=None, model=None,
                            usage_sink=None):
        resolve_calls.append(True)
        return ["assume X"]

    monkeypatch.setattr(ev, "evaluate_spec", _fail_evaluate)
    monkeypatch.setattr(ev, "resolve_assumptions", _fake_resolve)

    t = Task.new("clarify me", repo_path="/r")
    t.acceptance_criteria = ["stated one"]
    t.context = {"eval_result": {"verdict": "clarify", "dimensions": {}}}
    await store.create_task(t)
    orch = _orch(store, tmp_path)

    await orch._act_on_stored_eval(t)
    first = await store.get_task(t.id)
    assert first.context["eval_acted"] is True
    assert first.context["assumptions"] == ["assume X"]

    await orch._act_on_stored_eval(t)
    second = await store.get_task(t.id)

    assert not evaluate_calls
    assert len(resolve_calls) == 1
    assert second.context["assumptions"] == ["assume X"]
    assert second.acceptance_criteria == ["stated one"]


async def test_acting_on_stored_eval_idempotent_with_preseeded_original(store, tmp_path):
    """A second _act_on_eval pass (defense-in-depth guard, independent of the
    eval_acted marker) must not re-adopt when criteria are already the
    enriched ones and original_criteria is already recorded."""
    t = Task.new("do a thing", repo_path="/r")
    t.acceptance_criteria = ["stated one"]
    t.context = {"original_criteria": ["stated one"]}
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(verdict=EvalVerdict.ENRICH, enriched_criteria=["sharp A", "sharp B"])

    await orch._act_on_eval(t, eval_out)
    first = await store.get_task(t.id)
    assert first.acceptance_criteria == ["sharp A", "sharp B"]
    assert first.context["original_criteria"] == ["stated one"]

    # Simulate a second call with the same in-memory task/eval_out — the
    # already_adopted guard must skip the write and the emit.
    t.context = first.context
    t.acceptance_criteria = first.acceptance_criteria
    await orch._act_on_eval(t, eval_out)
    second = await store.get_task(t.id)
    assert second.acceptance_criteria == ["sharp A", "sharp B"]
    assert second.context["original_criteria"] == ["stated one"]


async def test_act_on_stored_eval_noop_verdict_marks_acted_without_calling_evaluate(
    store, tmp_path, monkeypatch
):
    """Mirrors test_intake_evaluator_skipped_when_already_evaluated: an accept
    verdict marks eval_acted and no-ops, without calling evaluate_spec."""
    evaluate_calls = []

    async def _fail_evaluate(*a, **k):
        evaluate_calls.append(True)
        raise AssertionError("evaluate_spec must not run on the stored-eval path")

    monkeypatch.setattr(ev, "evaluate_spec", _fail_evaluate)

    t = Task.new("already evaluated", repo_path="/r")
    t.context = {"eval_result": {"verdict": "accept"}}
    await store.create_task(t)
    orch = _orch(store, tmp_path)

    await orch._act_on_stored_eval(t)

    got = await store.get_task(t.id)
    assert got.context["eval_acted"] is True
    assert not evaluate_calls


# --------------------------------------------------------------------------- #
# AC 4: merge-not-clobber                                                     #
# --------------------------------------------------------------------------- #

async def test_concurrent_context_key_survives_act_on_eval(store, tmp_path, monkeypatch):
    async def _fake_resolve(title, description, criteria, *, backend=None, model=None,
                            usage_sink=None):
        # Simulate a concurrent writer landing a key AFTER _act_on_eval read
        # task.context but BEFORE it writes back.
        await store.merge_context(t.id, {"someone_elses_key": "kept"})
        return ["assume the header is X-Instance-Id"]

    monkeypatch.setattr(ev, "resolve_assumptions", _fake_resolve)

    t = Task.new("ambiguous task", repo_path="/r")
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(verdict=EvalVerdict.CLARIFY)

    await orch._act_on_eval(t, eval_out)

    got = await store.get_task(t.id)
    assert got.context["assumptions"] == ["assume the header is X-Instance-Id"]
    assert got.context["someone_elses_key"] == "kept"
