"""bench and board report pass^k and escalation LATENCY, not just green ticks.

Covers:
  - `Blocker.escalation_latency` round-trips through `to_dict`/`from_dict`,
    absent-means-None (never a fabricated zero).
  - `Orchestrator._raise_blocker` stamps `attempts_before_escalation` /
    `tokens_before_escalation` from `store.lifetime_usage` on every off-ramp,
    fails OPEN (telemetry only — never breaks routing) when that call raises.
  - `nh task show` prints "Escalated at attempt N after M tokens" only for an
    ESCALATED task that carries the latency.
  - The bench report's per-task table renders `attempts` / `tokens @
    escalation` columns, visually distinguishing escalated from
    non-escalated rows, and a `pass^k` cell present only under `--trials`.
  - `BenchScore.as_dict()` carries the two new fields (schema-population
    guard).
  - `docs/eval.md` records the pass^k investigation verdict and the
    `total_nh_tokens` validation check.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner

from no_human.blockers import Blocker, BlockerCategory
from no_human.cli.commands import cli
from no_human.config import load_config
from no_human.core.db import Store
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.eval.northstar import BenchScore
from no_human.eval.northstar_card import NorthStarCard, render_northstar_md
from no_human.notify.slack import SlackNotifier

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 1. Blocker.escalation_latency round-trip
# --------------------------------------------------------------------------- #

def test_blocker_round_trips_escalation_latency():
    b = Blocker(category=BlockerCategory.STAGNATION, goal="g", evidence="e")
    b.escalation_latency = {"attempts_before_escalation": 3,
                            "tokens_before_escalation": 4_200_000}
    d = b.to_dict()
    assert d["escalation_latency"] == {
        "attempts_before_escalation": 3, "tokens_before_escalation": 4_200_000}

    back = Blocker.from_dict(d)
    assert back.escalation_latency == {
        "attempts_before_escalation": 3, "tokens_before_escalation": 4_200_000}

    # A dict without the key rehydrates to None, not {...: 0}.
    d_no_key = dict(d)
    del d_no_key["escalation_latency"]
    assert Blocker.from_dict(d_no_key).escalation_latency is None

    # A raw Blocker() default is also None.
    assert Blocker(category=BlockerCategory.STAGNATION).escalation_latency is None


# --------------------------------------------------------------------------- #
# 2. Orchestrator._raise_blocker stamps attempts/tokens
# --------------------------------------------------------------------------- #

class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run in this test")


def _orch(store, tmp_path, sink=None):
    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=sink)


async def _new_task(store, title="a task"):
    t = Task.new(title, repo_path="/r")
    t.acceptance_criteria = ["keep me"]
    await store.create_task(t)
    return t


def _blocker():
    return Blocker(
        category=BlockerCategory.NOVEL_UNKNOWN,
        transient=False, confidence=0.9, goal="do the thing",
        root_cause_hypothesis="unknown", evidence="ev",
        question="what now?",
    )


def test_raise_blocker_stamps_attempts_and_tokens(tmp_path):
    async def _run():
        async with Store(tmp_path / "nh.db") as store:
            t = await _new_task(store)
            # Two attempt rows with known token columns.
            a1 = await store.create_attempt(t.id, 1)
            await store.update_attempt(a1, tokens_used=1_000)
            a2 = await store.create_attempt(t.id, 2)
            await store.update_attempt(a2, tokens_used=2_400)

            events = []
            orch = _orch(store, tmp_path, sink=events.append)
            blocker = _blocker()
            await orch._raise_blocker(t, blocker, escalate_now=True)

            got = await store.get_task(t.id)
            assert got.blocker["escalation_latency"] == {
                "attempts_before_escalation": 2,
                "tokens_before_escalation": 3_400,
            }
            esc_events = [e for e in events if e["kind"] == "escalated"]
            assert len(esc_events) == 1
            assert esc_events[0]["blocker"]["escalation_latency"] == {
                "attempts_before_escalation": 2,
                "tokens_before_escalation": 3_400,
            }
    asyncio.run(_run())


def test_latency_failure_does_not_break_the_off_ramp(tmp_path, monkeypatch):
    """Telemetry is fail-open; routing is not."""
    async def _run():
        async with Store(tmp_path / "nh.db") as store:
            t = await _new_task(store)

            async def _boom(task_id):
                raise RuntimeError("db exploded")
            monkeypatch.setattr(store, "lifetime_usage", _boom)

            orch = _orch(store, tmp_path)
            blocker = _blocker()
            outcome = await orch._raise_blocker(t, blocker, escalate_now=True)

            got = await store.get_task(t.id)
            assert got.status is TaskStatus.ESCALATED
            assert got.blocker.get("escalation_latency") is None
            assert outcome is not None
    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# 3. nh task show prints escalation context
# --------------------------------------------------------------------------- #

def _make_runner(path: Path, monkeypatch) -> CliRunner:
    import no_human.cli.commands as cmd_mod

    class _Cfg:
        primary_model = "claude-sonnet-4-6"
        review_model = "claude-sonnet-4-6"
        data: dict = {}

        def get(self, key, default=None):
            return self.data.get(key, default)

        def __getitem__(self, key):
            return self.data[key]

    _Cfg.db_path = path
    monkeypatch.setattr(cmd_mod, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cmd_mod, "assert_subscription_mode", lambda **kw: None)
    monkeypatch.setattr(
        cmd_mod, "_probe_pool",
        lambda _cfg: cmd_mod.PoolProbe(None, cmd_mod.POOL_REFUSED))
    return CliRunner()


def test_task_show_prints_escalation_context(tmp_path, monkeypatch):
    db = tmp_path / "test.db"

    async def _seed():
        async with Store(db) as s:
            t = Task.new("escalated task", repo_path="/tmp/r")
            t.id = "esc-task-1"
            await s.create_task(t)
            t.blocker = {
                "category": "NOVEL_UNKNOWN",
                "escalation_latency": {
                    "attempts_before_escalation": 3,
                    "tokens_before_escalation": 4_200_000,
                },
            }
            await s.update_task(t)
            await s.set_status(t, TaskStatus.ESCALATED, validate=False)
            return t.id

    task_id = asyncio.run(_seed())
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "show", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "Escalated at attempt 3 after 4,200,000 tokens" in result.output, result.output


def test_task_show_prints_nothing_for_a_non_escalated_task(tmp_path, monkeypatch):
    db = tmp_path / "test.db"

    async def _seed():
        async with Store(db) as s:
            t = Task.new("ordinary task", repo_path="/tmp/r")
            t.id = "ordinary-task-1"
            await s.create_task(t)
            return t.id

    task_id = asyncio.run(_seed())
    runner = _make_runner(db, monkeypatch)
    result = runner.invoke(cli, ["task", "show", task_id[:8]])

    assert result.exit_code == 0, result.output
    assert "Escalated at attempt" not in result.output, result.output


# --------------------------------------------------------------------------- #
# 4. Bench report — attempts / tokens @ escalation / pass^k columns
# --------------------------------------------------------------------------- #

def test_report_renders_attempts_and_tokens_columns():
    card = NorthStarCard.load(REPO_ROOT / "testdata" / "bench_escalation_latency.json")
    assert card is not None
    md = render_northstar_md(card)

    header_line = next(ln for ln in md.splitlines() if ln.startswith("| task |"))
    assert "attempts" in header_line, header_line
    assert "tokens @ escalation" in header_line, header_line

    sep_line = md.splitlines()[md.splitlines().index(header_line) + 1]
    header_cols = header_line.strip().count("|") - 1
    sep_cols = sep_line.strip().count("|") - 1
    assert header_cols == sep_cols, (header_line, sep_line)

    attempt1_row = next(ln for ln in md.splitlines()
                        if ln.startswith("| ns-escalated-attempt1 "))
    attempt3_row = next(ln for ln in md.splitlines()
                        if ln.startswith("| ns-escalated-attempt3 "))
    assert attempt1_row != attempt3_row
    assert "50,000" in attempt1_row
    assert "4,200,000" in attempt3_row

    non_escalated_row = next(ln for ln in md.splitlines()
                             if ln.startswith("| ns-satisfied-not-escalated "))
    cells = [c.strip() for c in non_escalated_row.strip("|").split("|")]
    # attempts / tokens @ escalation are the 8th/9th cells (1-indexed):
    # task | outcome | satisfied | nh tokens | orig tokens | cost ratio |
    # orig follow-ups (proxy) | attempts | tokens @ escalation | notes
    assert cells[7] == "—", non_escalated_row
    assert cells[8] == "—", non_escalated_row


def test_pass_k_cell_present_at_trials_gt_1_and_absent_at_1():
    def _sc(task_id, trial, satisfied):
        return BenchScore(
            task_id=task_id, title=task_id, outcome_status="done",
            goal_satisfied=satisfied, escalated_honestly=False, mergeable=True,
            nh_tokens=1_000, nh_cache_tokens=0, nh_cache_creation_tokens=0,
            nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=1_000,
            orig_cache_tokens=0, orig_cache_creation_tokens=0,
            orig_wall_clock_s=1.0, orig_corrections=0, subset="core",
            trial=trial)

    scores = [_sc("ns-a", 0, True), _sc("ns-a", 1, True), _sc("ns-a", 2, False)]
    card = NorthStarCard(scores=scores, created_at="2026-08-11", label="t",
                         trials=3)
    md = render_northstar_md(card)
    header_line = next(ln for ln in md.splitlines() if ln.startswith("| task |"))
    assert "pass^k" in header_line, header_line
    row = next(ln for ln in md.splitlines() if "ns-a #1" in ln)
    passes, ran_n = card.per_spec_passes["ns-a"]
    assert f"{passes}/{ran_n}" in row, row

    single = NorthStarCard(scores=[_sc("ns-b", 0, True)], created_at="2026-08-11",
                           label="t", trials=1)
    md1 = render_northstar_md(single)
    header1 = next(ln for ln in md1.splitlines() if ln.startswith("| task |"))
    assert "pass^k" not in header1, header1


# --------------------------------------------------------------------------- #
# 5. BenchScore.as_dict schema-population guard
# --------------------------------------------------------------------------- #

def test_bench_score_as_dict_carries_latency_fields():
    s = BenchScore(
        task_id="t", title="t", outcome_status="escalated",
        goal_satisfied=None, escalated_honestly=True, mergeable=None,
        nh_tokens=1, nh_cache_tokens=0, nh_cache_creation_tokens=0,
        nh_turns=1, nh_wall_clock_s=1.0, orig_tokens=1,
        orig_cache_tokens=0, orig_cache_creation_tokens=0,
        orig_wall_clock_s=1.0, orig_corrections=0,
        attempts_before_escalation=3, tokens_before_escalation=4_200_000,
    )
    d = s.as_dict()
    assert d["attempts_before_escalation"] == 3
    assert d["tokens_before_escalation"] == 4_200_000


# --------------------------------------------------------------------------- #
# 6. docs/eval.md records the pass^k verdict + the token check
# --------------------------------------------------------------------------- #

def test_docs_eval_records_the_pass_k_verdict_and_token_check():
    text = (REPO_ROOT / "docs" / "eval.md").read_text(encoding="utf-8")
    assert "total_nh_tokens" in text
    assert "success_rate must never lead" in text
    assert "instrument, not a target" in text
    assert "--trials" in text
    assert "SUPPORTED" in text
