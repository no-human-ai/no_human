"""§6 directive: EVERY task passes the intake grill before planning —
orchestrator wiring. Mirrors test_eval_acts.py's harness."""


from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.intake import evaluator as ev
from no_human.intake.evaluator import GrillQA
from no_human.notify.slack import SlackNotifier


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path, events, cfg_overlay=None):
    cfg = load_config(tmp_path / "config.yaml")
    if cfg_overlay:
        cfg.data.update(cfg_overlay)
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)


def _qa():
    return [
        GrillQA(question="Which file?", decision_it_changes="target",
                answer="src/x.py:1", source="repo-evidence"),
        GrillQA(question="Rotate the credential?", decision_it_changes="auth",
                answer="HUMAN-GATED: not self-answerable", carve_out="access"),
    ]


async def test_grill_runs_for_every_task_and_persists_qa(
        store, tmp_path, monkeypatch):
    seen = {}

    async def _fake_grill(title, description, criteria, repo_path, *,
                          backend=None, model=None, questions=None,
                          usage_sink=None, outcome_sink=None,
                          questions_outcome_sink=None, probe=True):
        seen["repo_path"] = repo_path
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/repo/x")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)

    got = await store.get_task(t.id)
    qa = got.context["intake_qa"]
    assert len(qa) == 2 and qa[0]["answer"] == "src/x.py:1"
    assert seen["repo_path"] == "/repo/x"
    kinds = [e.get("kind") for e in events]
    assert "intake_grill" in kinds
    grill_ev = events[kinds.index("intake_grill")]
    assert "1 human-gated" in grill_ev["text"]


async def test_grill_skipped_after_interactive_grill(store, tmp_path, monkeypatch):
    called = []

    async def _fake_grill(*a, **k):  # pragma: no cover
        called.append(1)
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    t.context = {"grill_complete": True}
    await store.create_task(t)
    orch = _orch(store, tmp_path, [])
    await orch._run_intake_grill(t)
    assert called == []


async def test_grill_config_gate_off(store, tmp_path, monkeypatch):
    called = []

    async def _fake_grill(*a, **k):  # pragma: no cover
        called.append(1)
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    orch = _orch(store, tmp_path, [], cfg_overlay={"intake": {"grill": False}})
    await orch._run_intake_grill(t)
    assert called == []


async def test_grill_failure_never_breaks_the_task(store, tmp_path, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("grill down")
    monkeypatch.setattr(ev, "grill_spec", _boom)

    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)  # must not raise
    got = await store.get_task(t.id)
    assert "intake_qa" not in (got.context or {})
    assert any(e.get("kind") == "advisory" for e in events)


async def test_retry_keeps_prior_qa_and_never_regrills(store, tmp_path, monkeypatch):
    """`nh task retry` resets to PENDING and re-walks the spine — the prior
    Q&A stands; the two grill sessions are never re-spent (review r1 f2)."""
    called = []

    async def _fake_grill(*a, **k):  # pragma: no cover
        called.append(1)
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    t.context = {"intake_qa": [{"question": "old?", "decision_it_changes": "",
                                "answer": "kept", "source": "human",
                                "carve_out": "none"}]}
    await store.create_task(t)
    orch = _orch(store, tmp_path, [])
    await orch._run_intake_grill(t)
    assert called == []
    got = await store.get_task(t.id)
    assert got.context["intake_qa"][0]["answer"] == "kept"


async def test_malformed_none_intake_section_degrades_advisory(
        store, tmp_path, monkeypatch):
    """`intake:` as an empty YAML section resolves to None — the gate must
    degrade advisory, never AttributeError the task (review r1 f4)."""
    called = []

    async def _fake_grill(*a, **k):
        called.append(1)
        return _qa()
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events, cfg_overlay={"intake": None})
    await orch._run_intake_grill(t)  # must not raise
    # None-section means "no override" → grill stays on (default True).
    assert called == [1]


async def test_all_empty_answers_emit_an_advisory(store, tmp_path, monkeypatch):
    """v10 drill: the answering failure was silent (log-only) — it must be an
    advisory event so doctor/task_events see it (the anti-silent-death rule)."""
    from no_human.intake.evaluator import GrillQA

    async def _fake_grill(*a, **k):
        return [GrillQA(question="q?", decision_it_changes="d")]  # unanswered
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)
    adv = [e for e in events if e.get("kind") == "advisory"]
    assert any("unanswered" in e.get("text", "") for e in adv)
    # The QA is still persisted (unanswered beats absent).
    got = await store.get_task(t.id)
    assert got.context["intake_qa"][0]["question"] == "q?"


# --------- the outcome must reach task_events, not only a log line ---------- #

_QUESTIONS_BLOCK = (
    'GRILL_JSON_START\n{"questions": [{"question": "Which file?",'
    ' "decision_it_changes": "target", "carve_out": "none"}]}\nGRILL_JSON_END'
)
_ANSWERS_BLOCK = (
    'GRILL_ANSWERS_START\n{"answers": [{"i": 0, "answer": "src/app.py:42",'
    ' "source": "repo-evidence"}]}\nGRILL_ANSWERS_END'
)


class _Scripted:
    """A backend that hands back scripted final_texts, standing in for the one
    grill_spec builds itself when the orchestrator passes none."""

    texts: list[str] = []
    prompts: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def run(self, prompt, *, cwd=None, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        _Scripted.prompts.append(prompt)
        return AgentResult(final_text=_Scripted.texts.pop(0), num_turns=1,
                           is_error=False, tokens_used=1, session_id="s",
                           stop_reason="end_turn")


async def test_answering_outcome_reaches_task_events(
        store, tmp_path, monkeypatch):
    """End-to-end through the REAL grill_spec — which also makes signature
    drift on the new kwarg fail loudly here instead of turning the whole grill
    into the silent no-op _run_intake_grill's TypeError handler produces."""
    from no_human.agent import claude_backend as cb

    _Scripted.texts = [_QUESTIONS_BLOCK, _ANSWERS_BLOCK]
    _Scripted.prompts = []
    monkeypatch.setattr(cb, "ClaudeBackend", _Scripted)

    t = Task.new("t", repo_path=str(tmp_path))
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)

    ga = [e for e in events if e.get("kind") == "grill_answering"]
    assert len(ga) == 1, [e.get("kind") for e in events]
    assert ga[0]["outcome"] == "parsed_first_try"
    assert ga[0]["answers_applied"] == 1
    # Its OWN kind: a pass that SUCCEEDED is not a degradation, and doctor.py
    # counts every "advisory" event as a silently-dead subsystem.
    assert not any(e.get("kind") == "advisory" for e in events)


async def test_a_failing_answering_pass_is_counted_not_just_logged(
        store, tmp_path, monkeypatch):
    """The whole point: a 0% answer rate must be READABLE, not inferable from
    a log line nothing asserts on."""
    from no_human.agent import claude_backend as cb

    _Scripted.texts = [_QUESTIONS_BLOCK, "no block at all", "still nothing"]
    _Scripted.prompts = []
    monkeypatch.setattr(cb, "ClaudeBackend", _Scripted)

    t = Task.new("t", repo_path=str(tmp_path))
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)

    ga = [e for e in events if e.get("kind") == "grill_answering"]
    assert len(ga) == 1 and ga[0]["outcome"] == "no_block_after_retry"
    assert ga[0]["answers_applied"] == 0


async def test_metrics_group_the_answering_outcomes(store, tmp_path):
    """Where the number is READ FROM: /api/metrics →
    grill_answering_outcomes, one SQL group-by over task_events (same shape as
    error_breakdown / repro_gate_verdicts)."""
    import time

    from no_human.core.metrics import compute_metrics

    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)

    def _ev(outcome=None, kind="grill_answering"):
        d = {"source": "orchestrator", "kind": kind, "text": "",
             "ts": time.time()}
        if outcome is not None:
            d["outcome"] = outcome
        return d

    await store.save_events(t.id, [
        _ev("parsed_first_try"),
        _ev("parsed_first_try"),
        _ev("no_block_after_retry"),
        _ev("unparseable_after_retry"),
        _ev(),                      # pre-instrumentation row
        _ev("parsed_first_try", kind="review"),  # other kinds must not count
    ])
    m = await compute_metrics(store)
    assert m["grill_answering_outcomes"] == {
        "parsed_first_try": 2, "no_block_after_retry": 1,
        "unparseable_after_retry": 1, "unclassified": 1,
    }


# ------- r2: the metric must be an ANSWER rate, and BOTH passes counted ----- #

_EMPTY_ANSWERS_BLOCK = (
    'GRILL_ANSWERS_START\n{"answers": []}\nGRILL_ANSWERS_END'
)
_BAD_QUESTIONS_BLOCK = (  # regex matches, contents are not JSON
    "GRILL_JSON_START\nhere are some questions for you\nGRILL_JSON_END"
)


async def test_a_pass_that_parses_but_answers_nothing_is_visible_end_to_end(
        store, tmp_path, monkeypatch):
    """The live symptom, through the real orchestrator: the block PARSES, zero
    answers are applied, and the outcome split says `parsed_first_try` — i.e.
    identical to a healthy run. Only `answers_applied` separates them, so it
    has to reach task_events, and the advisory has to fire alongside it."""
    from no_human.agent import claude_backend as cb

    _Scripted.texts = [_QUESTIONS_BLOCK, _EMPTY_ANSWERS_BLOCK]
    _Scripted.prompts = []
    monkeypatch.setattr(cb, "ClaudeBackend", _Scripted)

    t = Task.new("t", repo_path=str(tmp_path))
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)

    ga = [e for e in events if e.get("kind") == "grill_answering"]
    assert len(ga) == 1
    assert ga[0]["outcome"] == "parsed_first_try"   # looks healthy...
    assert ga[0]["answers_applied"] == 0            # ...and answered nothing
    assert ga[0]["answerable"] == 1
    assert ga[0]["timed_out"] is False
    # ...and the pre-existing silent-death advisory still fires beside it.
    assert any("unanswered" in e.get("text", "")
               for e in events if e.get("kind") == "advisory")


async def test_the_questions_pass_reaches_task_events(store, tmp_path,
                                                      monkeypatch):
    """It did not, before. A malformed questions block made `grill_spec`
    return None, the `if not qa: return` below fired ahead of the orchestrator's
    own advisory, and the whole grill vanished with ZERO events of any kind —
    the failure that produced the live "grill produced no parseable GRILL_JSON
    block" line with nothing countable behind it."""
    from no_human.agent import claude_backend as cb

    _Scripted.texts = [_BAD_QUESTIONS_BLOCK, _BAD_QUESTIONS_BLOCK]
    _Scripted.prompts = []
    monkeypatch.setattr(cb, "ClaudeBackend", _Scripted)

    t = Task.new("t", repo_path=str(tmp_path))
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)

    gq = [e for e in events if e.get("kind") == "grill_questions"]
    assert len(gq) == 1, [e.get("kind") for e in events]
    assert gq[0]["outcome"] == "unparseable_after_retry"
    assert gq[0]["questions"] == 0
    # The answering pass never ran, so it reports nothing — an absent pass is
    # not a failed one.
    assert not any(e.get("kind") == "grill_answering" for e in events)


async def test_a_healthy_questions_pass_is_counted_too(store, tmp_path,
                                                       monkeypatch):
    """A failure-only counter has no denominator."""
    from no_human.agent import claude_backend as cb

    _Scripted.texts = [_QUESTIONS_BLOCK, _ANSWERS_BLOCK]
    _Scripted.prompts = []
    monkeypatch.setattr(cb, "ClaudeBackend", _Scripted)

    t = Task.new("t", repo_path=str(tmp_path))
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    await orch._run_intake_grill(t)

    gq = [e for e in events if e.get("kind") == "grill_questions"]
    assert len(gq) == 1
    assert gq[0]["outcome"] == "parsed_first_try" and gq[0]["questions"] == 1
    assert not any(e.get("kind") == "advisory" for e in events)


async def test_metrics_separate_parsed_from_parsed_and_answered_nothing(store,
                                                                        tmp_path):
    """BLOCKER, r2: `grill_answering_outcomes` alone is a PARSE rate. A pass
    that parses and applies zero answers records as `parsed_first_try` and was
    indistinguishable at /api/metrics from a healthy one — `answers_applied`
    was written on every row and read by NOTHING. This is the reader."""
    import time

    from no_human.core.metrics import compute_metrics

    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)

    def _ev(outcome, applied=None, answerable=None,
            kind="grill_answering"):
        d = {"source": "orchestrator", "kind": kind, "text": "",
             "ts": time.time(), "outcome": outcome}
        if applied is not None:
            d["answers_applied"] = applied
        if answerable is not None:
            d["answerable"] = answerable
        return d

    await store.save_events(t.id, [
        _ev("parsed_first_try", 2, 2),               # healthy
        _ev("parsed_first_try", 0, 3),               # THE live symptom
        _ev("parsed_after_tool_less_retry", 1, 1),   # recovered
        # Never parsed: it has no answers to apply, so it must not be blamed
        # here — it is already counted in the outcome split.
        _ev("no_block_after_retry", 0, 4),
        _ev("error", 0, 2),
        # A row from before the field existed, and another kind entirely.
        _ev("parsed_first_try"),
        _ev("parsed_first_try", 9, 9, kind="review"),
    ])
    m = await compute_metrics(store)
    assert m["grill_answering_answers"] == {
        "parsed_passes": 4,           # 3 with figures + 1 pre-instrumentation
        "measured_passes": 3,
        "answers_applied": 3,
        "answerable": 6,
        # ONE — the 0-of-3 row. The pre-instrumentation row has no recorded
        # count and must not be accused of applying zero, or this key would
        # manufacture the very failure it exists to detect.
        "parsed_but_zero_applied": 1,
        "answer_rate": 0.5,
    }
    # ...while the outcome split still calls all four of those a parse.
    assert m["grill_answering_outcomes"]["parsed_first_try"] == 3


async def test_metrics_group_the_questions_outcomes(store, tmp_path):
    """Same shape for the questions pass, so its failures are countable
    instead of silent."""
    import time

    from no_human.core.metrics import compute_metrics

    t = Task.new("x", repo_path="/tmp/x")
    await store.create_task(t)

    def _ev(outcome, kind="grill_questions"):
        return {"source": "orchestrator", "kind": kind, "text": "",
                "ts": time.time(), "outcome": outcome}

    await store.save_events(t.id, [
        _ev("parsed_first_try"),
        _ev("unparseable_after_retry"),
        _ev("unparseable_after_retry"),
        _ev("empty_after_parse"),
        _ev("parsed_first_try", kind="grill_answering"),  # must not count
    ])
    m = await compute_metrics(store)
    assert m["grill_questions_outcomes"] == {
        "parsed_first_try": 1, "unparseable_after_retry": 2,
        "empty_after_parse": 1,
    }
