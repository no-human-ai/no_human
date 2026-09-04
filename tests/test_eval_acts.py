"""P2: the orchestrator ACTS on the intake evaluator verdict (enrich/clarify)
instead of only annotating it — so no human is needed mid-flight."""


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.intake import evaluator as ev
from no_human.intake.evaluator import EvalResult, EvalVerdict
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None))


async def test_enrich_adopts_criteria_and_preserves_original(store, tmp_path):
    t = Task.new("do a thing", repo_path="/r")
    t.acceptance_criteria = ["original one"]
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(
        verdict=EvalVerdict.ENRICH,
        enriched_criteria=["sharper A", "sharper B"],
    )
    await orch._act_on_eval(t, eval_out)

    got = await store.get_task(t.id)
    assert got.acceptance_criteria == ["sharper A", "sharper B"]
    assert got.context["original_criteria"] == ["original one"]


async def test_clarify_records_assumptions(store, tmp_path, monkeypatch):
    async def _fake_resolve(title, description, criteria, *, backend=None, model=None,
                            usage_sink=None):
        return ["assume the header is X-Instance-Id"]
    monkeypatch.setattr(ev, "resolve_assumptions", _fake_resolve)

    t = Task.new("ambiguous task", repo_path="/r")
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(verdict=EvalVerdict.CLARIFY)
    await orch._act_on_eval(t, eval_out)

    got = await store.get_task(t.id)
    assert got.context["assumptions"] == ["assume the header is X-Instance-Id"]


async def test_accept_verdict_is_noop(store, tmp_path):
    t = Task.new("clear task", repo_path="/r")
    t.acceptance_criteria = ["keep me"]
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    await orch._act_on_eval(t, EvalResult(verdict=EvalVerdict.ACCEPT))

    got = await store.get_task(t.id)
    assert got.acceptance_criteria == ["keep me"]
    assert "assumptions" not in (got.context or {})
    assert "original_criteria" not in (got.context or {})


async def test_enrich_preserves_empty_original_criteria(store, tmp_path):
    """A board-created task states NO criteria — exactly when ENRICH fires.
    original_criteria must record [] so the MoA complexity gate counts what
    the operator stated, not the evaluator's own enrichment (which fanned
    3 Opus proposers out on a kebab-case helper, task 6e64c555)."""
    t = Task.new("quick task", repo_path="/r")
    assert not t.acceptance_criteria
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(
        verdict=EvalVerdict.ENRICH,
        enriched_criteria=[f"enriched {i}" for i in range(6)],
    )
    await orch._act_on_eval(t, eval_out)

    got = await store.get_task(t.id)
    assert got.acceptance_criteria == [f"enriched {i}" for i in range(6)]
    assert got.context["original_criteria"] == []

    from no_human.core.orchestrator import _moa_complexity_signals
    assert _moa_complexity_signals(got, {"criteria_threshold": 5}) == []


async def test_missing_context_resolves_assumptions_even_on_accept(
    store, tmp_path, monkeypatch
):
    """v6 taxonomy: tasks that passed intake still parked mid-run on AMBIGUITY.
    An explicit no_missing_context=false is the evaluator saying 'the agent
    will hit an information gap' — resolve assumptions up front regardless of
    the headline verdict."""
    async def _fake_resolve(title, description, criteria, *, backend=None, model=None,
                            usage_sink=None):
        return ["assume the report endpoint means /api/reports/fetch"]
    monkeypatch.setattr(ev, "resolve_assumptions", _fake_resolve)

    t = Task.new("underspecified but clear task", repo_path="/r")
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(
        verdict=EvalVerdict.ACCEPT,
        dimensions={"clear_objective": True, "no_missing_context": False},
    )
    await orch._act_on_eval(t, eval_out)

    got = await store.get_task(t.id)
    assert got.context["assumptions"] == [
        "assume the report endpoint means /api/reports/fetch"]


async def test_missing_dimensions_default_to_no_assumptions(store, tmp_path):
    t = Task.new("clear task", repo_path="/r")
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    await orch._act_on_eval(t, EvalResult(verdict=EvalVerdict.ACCEPT, dimensions={}))
    got = await store.get_task(t.id)
    assert "assumptions" not in (got.context or {})


async def test_decompose_attaches_split_proposal(store, tmp_path, monkeypatch):
    """SCRUM-36: a DECOMPOSE verdict attaches a non-binding split proposal
    to task.context via the same guarded/deduped seam as SCOPE_EXPLOSION —
    never mutating title/description/acceptance criteria, never creating a
    task."""
    calls = []

    async def fake_generate(task, files_to_change=None, surfaces=None, **kw):
        calls.append(task.id)
        return "Split proposal:\n\n1. Part one\nDo it.\nContract: c1"

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = Task.new("oversized task", repo_path="/r")
    t.acceptance_criteria = ["keep me"]
    before_title, before_criteria = t.title, list(t.acceptance_criteria)
    before_count = len(await store.list_tasks())
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(
        verdict=EvalVerdict.DECOMPOSE,
        dimensions={"bounded_scope": False},
    )
    await orch._act_on_eval(t, eval_out)

    got = await store.get_task(t.id)
    assert got.context["split_proposal"].startswith("Split proposal:")
    assert calls == [t.id]
    # advisory only: no task mutation, no auto-created task.
    assert got.title == before_title
    assert got.acceptance_criteria == before_criteria
    assert len(await store.list_tasks()) == before_count + 1


async def test_decompose_generator_none_skips_silently(store, tmp_path, monkeypatch):
    async def fake_generate(*a, **k):
        return None

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = Task.new("oversized task", repo_path="/r")
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    await orch._act_on_eval(t, EvalResult(verdict=EvalVerdict.DECOMPOSE))

    got = await store.get_task(t.id)
    assert "split_proposal" not in (got.context or {})


async def test_decompose_existing_proposal_blocks_regeneration(
    store, tmp_path, monkeypatch
):
    """Dedupe: a task with a split_proposal already in context (e.g. from an
    earlier SCOPE_EXPLOSION blocker) must not regenerate on a later DECOMPOSE
    verdict — one proposal per task unless the human clears it."""
    calls = []

    async def fake_generate(*a, **k):
        calls.append(True)
        return "SHOULD NOT BE STORED"

    monkeypatch.setattr(
        "no_human.core.orchestrator.generate_split_proposal", fake_generate
    )

    t = Task.new("oversized task", repo_path="/r")
    t.context = {"split_proposal": "EXISTING PROPOSAL"}
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    await orch._act_on_eval(t, EvalResult(verdict=EvalVerdict.DECOMPOSE))

    got = await store.get_task(t.id)
    assert got.context["split_proposal"] == "EXISTING PROPOSAL"
    assert not calls


async def test_enrich_with_missing_context_does_both(store, tmp_path, monkeypatch):
    async def _fake_resolve(title, description, criteria, *, backend=None, model=None,
                            usage_sink=None):
        return ["assume X"]
    monkeypatch.setattr(ev, "resolve_assumptions", _fake_resolve)

    t = Task.new("enrichable underspecified task", repo_path="/r")
    t.acceptance_criteria = ["original"]
    await store.create_task(t)
    orch = _orch(store, tmp_path)
    eval_out = EvalResult(
        verdict=EvalVerdict.ENRICH,
        enriched_criteria=["sharper"],
        dimensions={"no_missing_context": False},
    )
    await orch._act_on_eval(t, eval_out)

    got = await store.get_task(t.id)
    assert got.acceptance_criteria == ["sharper"]
    assert got.context["assumptions"] == ["assume X"]
