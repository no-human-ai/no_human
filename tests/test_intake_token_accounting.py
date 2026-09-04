"""Intake spend reaches a ledger instead of vanishing at the type boundary.

Six utility-tier call sites (spec evaluator, assumption resolver, both halves of
the intake grill, the split-proposal drafter, and the interactive GUI/CLI grill)
each spent real tokens and recorded none of them: the functions return verdicts,
assumptions, Q&A and prose — never an ``AgentResult`` — so the three token
figures were dropped on the way back.

Every test here ends at a PERSISTED ROW read back out of SQLite, and the
expected numbers are literals fixed by the fake backend, never recomputed
through the code under test. Two of the wirings are mutation-checked in
``test_mutation_*`` below: drop the assignment and the row goes back to zero.
"""

from __future__ import annotations

import subprocess

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.agent.claude_backend import AgentResult
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.intake.evaluator import EvalResult, EvalVerdict
from no_human.notify.slack import SlackNotifier

# One call's bill. Distinctive values so a row that holds them cannot have
# gotten them from anywhere else in the pipeline.
IO, CR, CC = 1_001, 2_002, 303


class _TokenBackend:
    """A ClaudeBackend stand-in that reports a known bill on every call.

    Scripted ``texts`` are returned in order so the intake parsers see whatever
    block the site under test needs; an exhausted script returns "" (which the
    advisory contract treats as "no result"), and the call is still billed —
    that is the point: a call that produced nothing usable still cost money.
    """

    def __init__(self, texts=(), *, tokens=(IO, CR, CC)):
        self._texts = list(texts)
        self._tokens = tokens
        self.calls = 0

    async def run(self, prompt, **kwargs):
        self.calls += 1
        text = self._texts.pop(0) if self._texts else ""
        io, cr, cc = self._tokens
        return AgentResult(
            final_text=text, num_turns=1, is_error=False, tokens_used=io,
            session_id="s", stop_reason="end_turn",
            cache_read_tokens=cr, cache_creation_tokens=cc,
        )


def _patch_backend(monkeypatch, texts=()):
    """Replace the ClaudeBackend the intake sites construct for themselves.

    The source module is the seam: every intake entry point does a lazy
    ``from ..agent.claude_backend import ClaudeBackend`` at CALL time, so this
    is the attribute they resolve against (same reasoning as conftest's
    hermetic patch, which this deliberately overrides with a billed backend).
    """
    inst = _TokenBackend(texts)
    monkeypatch.setattr(
        "no_human.agent.claude_backend.ClaudeBackend", lambda *a, **k: inst)
    return inst


QUESTIONS_BLOCK = (
    'GRILL_JSON_START {"questions": [{"question": "which module?", '
    '"decision_it_changes": "target file", "carve_out": "none"}]} GRILL_JSON_END'
)
GATED_QUESTIONS_BLOCK = (
    'GRILL_JSON_START {"questions": [{"question": "may I rotate the token?", '
    '"decision_it_changes": "credentials", "carve_out": "access"}]} '
    "GRILL_JSON_END"
)
ANSWERS_BLOCK = (
    'GRILL_ANSWERS_START {"answers": [{"i": 0, "answer": "the core module", '
    '"source": "assumption"}]} GRILL_ANSWERS_END'
)
ASSUMPTIONS_BLOCK = (
    'ASSUMPTIONS_JSON_START {"assumptions": ["the header is X-Instance-Id"]} '
    "ASSUMPTIONS_JSON_END"
)
_SENTENCES = "One thing. Two thing. Three thing."
SPLIT_BLOCK = (
    'SPLIT_JSON_START [{"title": "a", "description": "%s", "contract": "c1"}, '
    '{"title": "b", "description": "%s", "contract": "c2"}] SPLIT_JSON_END'
    % (_SENTENCES, _SENTENCES)
)
EVAL_BLOCK = (
    'EVAL_JSON_START {"verdict": "accept", "dimensions": {"clear_objective": '
    'true, "testable_criteria": true, "bounded_scope": true, '
    '"no_missing_context": true}, "rationale": "fine"} EVAL_JSON_END'
)


class _NoBackend:
    async def run(self, *a, **k):  # pragma: no cover — must never be reached
        raise AssertionError("the coder backend should not run in this test")


def _orch(store, tmp_path, backend=None):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    return Orchestrator(store, cfg.data, backend or _NoBackend(),
                        SlackNotifier(None))


async def _task(store, title="do a thing", repo="/r"):
    t = Task.new(title, repo_path=repo)
    await store.create_task(t)
    return t


async def _drain_to_attempt(orch, store, task):
    """Run the production drain and return the persisted attempt row.

    ``_pop_aux_usage()`` splatted into ``update_attempt`` is verbatim what every
    attempt-exit path in the orchestrator does; the assertion then reads the
    row back out of SQLite rather than trusting the accumulator.
    """
    attempt_id = await store.create_attempt(task.id, 1)
    await store.update_attempt(attempt_id, **orch._pop_aux_usage())
    rows = await store.list_attempts(task.id)
    return rows[-1]


# --------------------------------------------------------------------------- #
# Site 2: resolve_assumptions, via _act_on_eval (orchestrator production path)  #
# --------------------------------------------------------------------------- #

async def test_resolve_assumptions_spend_lands_on_the_attempt_row(
    store, tmp_path, monkeypatch,
):
    be = _patch_backend(monkeypatch, [ASSUMPTIONS_BLOCK])
    orch = _orch(store, tmp_path)
    t = await _task(store)

    await orch._act_on_eval(t, EvalResult(verdict=EvalVerdict.CLARIFY))

    assert be.calls == 1
    # The call did its job — accounting did not change what it does.
    assert (await store.get_task(t.id)).context["assumptions"] == [
        "the header is X-Instance-Id"]
    row = await _drain_to_attempt(orch, store, t)
    assert row["utility_tokens_used"] == IO
    assert row["utility_cache_read_tokens"] == CR
    assert row["utility_cache_creation_tokens"] == CC


# --------------------------------------------------------------------------- #
# Sites 3 + 4: the intake grill's questions and answers passes                  #
# --------------------------------------------------------------------------- #

async def test_grill_questions_pass_alone_lands_on_the_attempt_row(
    store, tmp_path, monkeypatch,
):
    """A grill whose only question is human-gated returns before the answering
    pass — so exactly ONE call happened and exactly one call's bill is on the
    row. That isolates the questions site from the answers site below."""
    be = _patch_backend(monkeypatch, [GATED_QUESTIONS_BLOCK])
    orch = _orch(store, tmp_path)
    t = await _task(store)

    await orch._run_intake_grill(t)

    assert be.calls == 1
    row = await _drain_to_attempt(orch, store, t)
    assert row["utility_tokens_used"] == IO
    assert row["utility_cache_read_tokens"] == CR
    assert row["utility_cache_creation_tokens"] == CC


async def test_grill_answers_pass_adds_its_own_bill(store, tmp_path, monkeypatch):
    """The expensive site (``max_turns=8``, exploring a live repo). With an
    answerable question BOTH calls run, and the row carries both — one call's
    worth would mean the answering pass is still free."""
    be = _patch_backend(monkeypatch, [QUESTIONS_BLOCK, ANSWERS_BLOCK])
    orch = _orch(store, tmp_path)
    t = await _task(store)

    await orch._run_intake_grill(t)

    assert be.calls == 2
    qa = (await store.get_task(t.id)).context["intake_qa"]
    assert qa[0]["answer"] == "the core module"
    row = await _drain_to_attempt(orch, store, t)
    assert row["utility_tokens_used"] == 2 * IO
    assert row["utility_cache_read_tokens"] == 2 * CR
    assert row["utility_cache_creation_tokens"] == 2 * CC


async def test_grill_retries_are_billed_too(store, tmp_path, monkeypatch):
    """Both parse retries are separate API calls. An unparseable questions
    block retries once, and an unparseable answers block retries tool-lessly —
    four calls, four bills. Booking only the last one would under-report the
    failures, which are exactly the expensive cases."""
    be = _patch_backend(monkeypatch, ["junk", QUESTIONS_BLOCK, "junk", ANSWERS_BLOCK])
    orch = _orch(store, tmp_path)
    t = await _task(store)

    await orch._run_intake_grill(t)

    assert be.calls == 4
    row = await _drain_to_attempt(orch, store, t)
    assert row["utility_tokens_used"] == 4 * IO
    assert row["utility_cache_read_tokens"] == 4 * CR


# --------------------------------------------------------------------------- #
# Site 5: generate_split_proposal, via _act_on_eval(DECOMPOSE)                  #
# --------------------------------------------------------------------------- #

async def test_split_proposal_spend_lands_on_the_attempt_row(
    store, tmp_path, monkeypatch,
):
    be = _patch_backend(monkeypatch, [SPLIT_BLOCK])
    orch = _orch(store, tmp_path)
    t = await _task(store)

    await orch._act_on_eval(t, EvalResult(verdict=EvalVerdict.DECOMPOSE))

    assert be.calls == 1
    assert "Split proposal:" in (await store.get_task(t.id)).context["split_proposal"]
    row = await _drain_to_attempt(orch, store, t)
    assert row["utility_tokens_used"] == IO
    assert row["utility_cache_read_tokens"] == CR
    assert row["utility_cache_creation_tokens"] == CC


# --------------------------------------------------------------------------- #
# Site 1: evaluate_spec, on the real _drive pipeline                            #
# --------------------------------------------------------------------------- #

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   text=True)


@pytest.fixture
def work_repo(tmp_path):
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
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


class _CoderBackend:
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


@pytest.mark.slow
async def test_evaluate_spec_spend_reaches_the_attempt_row_end_to_end(
    work_repo, tmp_path, store, monkeypatch,
):
    """The whole pipeline, real ``_drive``: the intake evaluator's bill is on
    the attempt row it fed. The grill is switched off so the ONLY utility-tier
    call in the run is the evaluator — the row's value is therefore that one
    call's, not an aggregate that could have come from anywhere."""
    be = _patch_backend(monkeypatch, [EVAL_BLOCK])
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("intake", {})["grill"] = False
    orch = Orchestrator(store, cfg.data, _CoderBackend(), SlackNotifier(None))

    t = Task.new("add mul()", repo_path=str(work_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(t)

    outcome = await orch.run_task(t)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL
    assert be.calls == 1
    assert (await store.get_task(t.id)).context["eval_result"]["verdict"] == "accept"

    rows = await store.list_attempts(t.id)
    assert rows[-1]["utility_tokens_used"] == IO
    assert rows[-1]["utility_cache_read_tokens"] == CR
    assert rows[-1]["utility_cache_creation_tokens"] == CC


# --------------------------------------------------------------------------- #
# Site 6: the GUI intake wizard — no task exists, so no attempt row exists      #
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def client(store, tmp_path):
    from no_human.api.app import app
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    app.state._grill_sessions = {}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


async def test_gui_intake_spend_is_booked_to_the_unattributed_ledger(
    client, store, monkeypatch,
):
    """``POST /api/grill`` runs before any task exists. There is no attempt row
    to bill, so the spend goes to ``unattributed_usage`` with ``task_id`` NULL —
    recorded honestly rather than forced onto an unrelated task."""
    _patch_backend(monkeypatch, ['```json\n{"type": "question", '
                                 '"question": "which module?", '
                                 '"suggestions": ["A: core"]}\n```'])
    r = await client.post("/api/grill", json={"title": "Fix X", "qa_history": []})
    assert r.status_code == 200

    cur = await store.db.execute(
        "SELECT site, task_id, tokens_used, cache_read_tokens, "
        "cache_creation_tokens FROM unattributed_usage")
    rows = [dict(x) for x in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["site"] == "api.grill"
    assert rows[0]["task_id"] is None
    assert (rows[0]["tokens_used"], rows[0]["cache_read_tokens"],
            rows[0]["cache_creation_tokens"]) == (IO, CR, CC)


# --------------------------------------------------------------------------- #
# Site 6b: the CLI intake wizard — same problem, same ledger                    #
# --------------------------------------------------------------------------- #

async def test_cli_intake_spend_is_booked_to_the_unattributed_ledger(
    store, tmp_path, monkeypatch,
):
    """``_run_cli_grill`` loops until the grill emits a final spec; the task is
    created only afterwards (and the operator may Ctrl-C first), so each round
    is booked with ``task_id`` NULL as it happens."""
    from no_human.cli import commands as cli

    inst = _TokenBackend([
        '```json\n{"type": "done", "title": "Fix X", "description": "d", '
        '"acceptance_criteria": ["ac1"]}\n```'])
    monkeypatch.setattr(cli, "ClaudeBackend", lambda *a, **k: inst)
    cfg = load_config(tmp_path / "config.yaml")

    t = Task.new("Fix X", repo_path="/r")
    out = await cli._run_cli_grill(cfg, t, store)
    assert out.context["grill_complete"] is True

    cur = await store.db.execute(
        "SELECT site, task_id, tokens_used, cache_read_tokens FROM unattributed_usage")
    rows = [dict(x) for x in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["site"] == "cli.task_add.grill"
    assert rows[0]["task_id"] is None
    assert (rows[0]["tokens_used"], rows[0]["cache_read_tokens"]) == (IO, CR)


# --------------------------------------------------------------------------- #
# The residual: pre-attempt intake on a task that never reaches an attempt      #
# --------------------------------------------------------------------------- #

async def test_intake_spend_on_a_task_with_no_attempt_is_not_lost(
    store, tmp_path, monkeypatch,
):
    """``_pop_aux_usage`` only runs on attempt EXITS. A task that parks at the
    plan gate, escalates on an unavailable input, or is decomposed never has
    one — so its intake bill used to evaporate. ``_drive_watched``'s finally
    flushes the undrained accumulator to the unattributed ledger instead."""
    be = _patch_backend(monkeypatch, [ASSUMPTIONS_BLOCK])
    orch = _orch(store, tmp_path)
    t = await _task(store)

    await orch._act_on_eval(t, EvalResult(verdict=EvalVerdict.CLARIFY))
    assert be.calls == 1
    await orch._flush_orphaned_aux_usage(t)

    cur = await store.db.execute(
        "SELECT site, task_id, tokens_used, cache_read_tokens, "
        "cache_creation_tokens FROM unattributed_usage")
    rows = [dict(x) for x in await cur.fetchall()]
    assert len(rows) == 1
    assert rows[0]["site"] == "orphaned_utility_usage"
    assert rows[0]["task_id"] == t.id
    assert (rows[0]["tokens_used"], rows[0]["cache_read_tokens"],
            rows[0]["cache_creation_tokens"]) == (IO, CR, CC)


async def test_flush_is_idempotent_and_never_double_books(
    store, tmp_path, monkeypatch,
):
    """The drain POPS. A task that did reach an attempt has already booked its
    intake to the attempt row, so the flush that follows must find nothing —
    otherwise every task would be billed twice."""
    _patch_backend(monkeypatch, [ASSUMPTIONS_BLOCK])
    orch = _orch(store, tmp_path)
    t = await _task(store)

    await orch._act_on_eval(t, EvalResult(verdict=EvalVerdict.CLARIFY))
    row = await _drain_to_attempt(orch, store, t)
    assert row["utility_tokens_used"] == IO

    await orch._flush_orphaned_aux_usage(t)
    assert (await store.unattributed_usage_totals())["total"] == 0


async def test_zero_usage_writes_no_ledger_row(store):
    """The ledger holds spend, never padding — a call that reports nothing
    leaves no row, so a non-empty table always means real money."""
    assert await store.record_unattributed_usage(site="api.grill") is None
    assert (await store.unattributed_usage_totals())["calls"] == 0


# --------------------------------------------------------------------------- #
# Mutation checks: drop the wiring, the ledger goes back to zero                #
# --------------------------------------------------------------------------- #

async def test_mutation_grill_spec_without_a_sink_records_nothing(
    store, tmp_path, monkeypatch,
):
    """Mutate site 3/4: call the production ``grill_spec`` with the usage_sink
    argument removed — the answers still arrive, the ledger stays empty. This
    is what the pre-fix code did, and it is why this test must fail if the
    orchestrator's ``usage_sink=self._note_utility_usage`` is ever dropped."""
    from no_human.intake import evaluator as ev

    be = _patch_backend(monkeypatch, [QUESTIONS_BLOCK, ANSWERS_BLOCK])
    orch = _orch(store, tmp_path)
    t = await _task(store)

    real = ev.grill_spec

    async def _sinkless(*a, **kw):
        kw.pop("usage_sink", None)          # the mutation
        return await real(*a, **kw)

    monkeypatch.setattr(ev, "grill_spec", _sinkless)
    await orch._run_intake_grill(t)

    assert be.calls == 2                    # the tokens were really spent
    row = await _drain_to_attempt(orch, store, t)
    assert row["utility_tokens_used"] == 0  # ...and nothing recorded them


async def test_mutation_grill_step_without_stamped_usage_records_nothing(
    client, store, monkeypatch,
):
    """Mutate site 6: strip the token fields ``grill_step`` stamps onto its
    result, and the GUI's ledger write finds nothing to book."""
    from no_human.api import app as api_app
    from no_human.intake import grill as grill_mod

    _patch_backend(monkeypatch, ['```json\n{"type": "question", '
                                 '"question": "q?", "suggestions": []}\n```'])
    real = grill_mod.grill_step

    async def _unstamped(*a, **kw):
        step = await real(*a, **kw)
        step.tokens_used = 0                # the mutation
        step.cache_read_tokens = 0
        step.cache_creation_tokens = 0
        return step

    monkeypatch.setattr(api_app, "grill_step", _unstamped, raising=False)
    monkeypatch.setattr(grill_mod, "grill_step", _unstamped)

    r = await client.post("/api/grill", json={"title": "Fix X", "qa_history": []})
    assert r.status_code == 200
    assert (await store.unattributed_usage_totals())["calls"] == 0


# --------------------------------------------------------------------------- #
# The WIRING, not the helper: run_task must flush on a task with no attempt     #
# --------------------------------------------------------------------------- #
# The two tests above call `_flush_orphaned_aux_usage` directly. That proves the
# helper computes the right thing and proves NOTHING about whether anything ever
# calls it — deleting the `await self._flush_orphaned_aux_usage(task)` line from
# `_drive_watched`'s finally left all of them green. These two go through
# `run_task`, so they break when the wiring breaks while the helper stays
# correct.

@pytest.mark.slow
async def test_run_task_flushes_intake_spend_when_no_attempt_ever_ran(
    work_repo, tmp_path, store, monkeypatch,
):
    """A task parked on the plan-approval gate never reaches an attempt, so
    nothing ever drains the accumulator. Driven through the real ``run_task``:
    the ledger row is the artifact, and no attempt row exists to hold it."""
    from no_human.core import plan_gate

    be = _patch_backend(monkeypatch, [EVAL_BLOCK])
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("intake", {})["grill"] = False
    orch = Orchestrator(store, cfg.data, _CoderBackend(), SlackNotifier(None))

    t = Task.new("add mul()", repo_path=str(work_repo))
    t.acceptance_criteria = ["mul(a,b) returns a*b"]
    t.config[plan_gate.CONFIG_KEY] = True          # park before any attempt
    await store.create_task(t)

    await orch.run_task(t)

    assert be.calls == 1, "the intake evaluator really ran and really billed"
    assert await store.count_attempts(t.id) == 0, "no attempt row to bill"

    cur = await store.db.execute(
        "SELECT site, task_id, tokens_used, cache_read_tokens, "
        "cache_creation_tokens FROM unattributed_usage")
    rows = [dict(x) for x in await cur.fetchall()]
    assert [r["site"] for r in rows] == ["orphaned_utility_usage"]
    assert rows[0]["task_id"] == t.id
    assert (rows[0]["tokens_used"], rows[0]["cache_read_tokens"],
            rows[0]["cache_creation_tokens"]) == (IO, CR, CC)


@pytest.mark.slow
async def test_run_task_does_not_leak_one_tasks_intake_onto_the_next(
    work_repo, tmp_path, store, monkeypatch,
):
    """LATENT guard, not a live bug: every shipped ``run_task`` call site builds
    a fresh Orchestrator for exactly one task, so nothing reuses the
    accumulator today. Reusing one here is the only way to observe the second
    thing the flush does — without it, task A's parked intake bill rides into
    task B's first attempt row, and B is billed for work it never asked for."""
    from no_human.core import plan_gate

    be = _patch_backend(monkeypatch, [EVAL_BLOCK, EVAL_BLOCK])
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    cfg.data.setdefault("intake", {})["grill"] = False
    orch = Orchestrator(store, cfg.data, _CoderBackend(), SlackNotifier(None))

    a = Task.new("parked task", repo_path=str(work_repo))
    a.acceptance_criteria = ["never gets an attempt"]
    a.config[plan_gate.CONFIG_KEY] = True
    await store.create_task(a)
    await orch.run_task(a)
    assert await store.count_attempts(a.id) == 0

    b = Task.new("add mul()", repo_path=str(work_repo))
    b.acceptance_criteria = ["mul(a,b) returns a*b"]
    await store.create_task(b)
    outcome = await orch.run_task(b)
    assert outcome.status is TaskStatus.AWAITING_APPROVAL

    assert be.calls == 2, "one intake call per task"
    rows = await store.list_attempts(b.id)
    # B's own single intake call, NOT A's as well.
    assert rows[-1]["utility_tokens_used"] == IO
    assert rows[-1]["utility_cache_read_tokens"] == CR
    assert rows[-1]["utility_cache_creation_tokens"] == CC


def test_nh_status_json_exposes_the_unattributed_residual(tmp_path):
    """A board-only operator reads `nh status --json`, and the GUI wizard is the
    biggest producer of unattributed rows — so the residual has to survive the
    --json branch, which used to return before ever reading it.

    Synchronous on purpose: the command calls ``asyncio.run`` itself.
    """
    import asyncio
    import json as _json

    from click.testing import CliRunner

    from no_human.cli import commands as cli

    db = tmp_path / "nh.db"

    async def _seed():
        s = await Store(db).connect()
        await s.record_unattributed_usage(
            site="api.grill", tokens_used=IO, cache_read_tokens=CR,
            cache_creation_tokens=CC)
        await s.close()

    asyncio.run(_seed())

    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["database"]["path"] = str(db)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(cli, "_bootstrap", lambda *a, **k: (cfg, None))
    try:
        res = CliRunner().invoke(cli.cli, ["status", "--json"])
    finally:
        monkey.undo()

    assert res.exit_code == 0, res.output
    payload = _json.loads(res.output)
    assert payload["unattributed_usage"]["calls"] == 1
    assert payload["unattributed_usage"]["total"] == IO + CR + CC
    # the pre-existing bucket keys are untouched
    assert payload["needs you"] == 0 and payload["done"] == 0
