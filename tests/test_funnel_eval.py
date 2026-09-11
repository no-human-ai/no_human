"""The nightly funnel eval runner (Phase C, tasks C3 + C4).

The backend is mocked at the seam every existing harness mocks it at — the
``backend_factory`` constructor kwarg (`eval/northstar.py` NorthStarRunner,
`eval/replay.py` ReplayRunner, `eval/harness.py` run_eval). Nothing here
monkeypatches the Agent SDK, and nothing here spends a token.

The devil's-advocate answers are the tests, not the docstring: a stalled tier
is KILLED and RECORDED rather than wedging the night, a crash is a nonzero
exit, the run happens in its own HOME, and the runner REFUSES to start when
the corpus can cost more than the night's budget.
"""

import dataclasses
import json
import re
from pathlib import Path

import pytest

from no_human.eval.funnel_corpus import load_corpus
from no_human.eval.funnel_eval import (
    compare_to_baseline, corpus_ceiling_tokens, run_funnel_eval,
)

pytestmark = pytest.mark.usefixtures("isolated_env_file")


# --------------------------------------------------------------------------- #
# Fake backends — the same duck type tests/test_northstar.py::_FixBackend uses #
# --------------------------------------------------------------------------- #

T2_FIXED = '''\
"""The store: add items, list them, mark them done."""


class UnknownItem(KeyError):
    """Raised when an id is not in the store."""


class Store:
    def __init__(self):
        self._items = {}
        self._next_id = 1

    def add(self, title):
        item = {"id": self._next_id, "title": title, "done": False}
        self._items[item["id"]] = item
        self._next_id += 1
        return item

    def complete(self, item_id):
        if item_id not in self._items:
            raise UnknownItem(item_id)
        self._items[item_id]["done"] = True
        return self._items[item_id]

    def all(self):
        return list(self._items.values())
'''

T3_SEARCH = '''

    def search(self, term):
        needle = term.lower()
        return [i for i in self._items.values() if needle in i["title"].lower()]
'''


class _TierBackend:
    """Applies this tier's fix. ``t4_cross_file`` STALLS forever instead — the
    hung Agent-SDK session the hard wall clock exists to survive. Any other
    name is a coder that ships a COSMETIC change and reports done — a real
    diff, so the run reaches the reviewer, with none of the asked-for
    behaviour. That is the subject of the "a passing reviewer cannot fake a
    green holdout" test."""

    def __init__(self, tier: str):
        self.tier = tier

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        import asyncio

        from no_human.agent.claude_backend import AgentResult

        work = Path(cwd)
        if self.tier == "t1_docs_oneliner":
            readme = work / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8").replace(
                "at most 100 items", "at most 200 items"))
        elif self.tier == "t2_small_fix":
            (work / "tinytodo" / "store.py").write_text(T2_FIXED)
        elif self.tier == "t3_small_feature":
            store = work / "tinytodo" / "store.py"
            store.write_text(store.read_text(encoding="utf-8").rstrip("\n") + T3_SEARCH)
        elif self.tier == "t4_cross_file":
            # The hung Agent-SDK session this whole harness exists to survive.
            # Cancellable, so the hard wall-clock can actually kill it.
            await asyncio.sleep(3600)
        else:
            store = work / "tinytodo" / "store.py"
            store.write_text(store.read_text(encoding="utf-8") + "\n# tidied up.\n")
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=1200, session_id="s",
                           stop_reason="end_turn")


class _PassReviewer:
    async def review(self, task, *, repo_path, test_output="",
                     held_out_output="", before_ref="HEAD~1",
                     after_ref="HEAD", **kwargs):
        from no_human.review.reviewer import ReviewDecision
        return ReviewDecision(passed=True, raw_output="scripted pass")


def _four_tier_corpus():
    """t1-t4 with the stalled tier's wall-clock ceiling cut to 2s, so the hard
    kill is exercised in seconds rather than an hour."""
    tasks = [t for t in load_corpus() if t.name != "t5_test_first"]
    for t in tasks:
        seconds = 2 if t.name == "t4_cross_file" else 900
        t.criteria = dataclasses.replace(t.criteria, max_wall_seconds=seconds)
    return tasks


# --------------------------------------------------------------------------- #
# C3 — the runner                                                              #
# --------------------------------------------------------------------------- #

def test_it_refuses_to_start_when_the_corpus_can_outspend_the_night(tmp_path):
    """The budget guard runs BEFORE anything is materialized: a refusal that
    happens after three tiers have already run has not refused anything."""
    started = []

    def factory(task):
        started.append(task.name)
        return _TierBackend(task.name)

    rc = run_funnel_eval(
        tmp_path / "home", tmp_path / "out",
        backend_factory=factory, reviewer=_PassReviewer(),
        corpus=_four_tier_corpus(), nightly_budget_tokens=1_000,
    )

    assert rc == 1
    assert started == [], "it must refuse before running anything"
    summary = (tmp_path / "out" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "REFUSED" in summary
    assert "8,900,000" in summary, summary   # the corpus ceiling, named
    assert "1,000" in summary                # and the budget it exceeds


def test_a_crash_is_a_nonzero_exit_not_a_green_night(tmp_path):
    def factory(task):
        raise RuntimeError("backend construction blew up")

    rc = run_funnel_eval(tmp_path / "home", tmp_path / "out",
                         backend_factory=factory, reviewer=_PassReviewer(),
                         corpus=_four_tier_corpus()[:1])

    assert rc == 1
    report = json.loads(next((tmp_path / "out").glob("nightly-*.json")).read_text(encoding="utf-8"))
    assert report["tasks"][0]["stage"] == "crashed"
    assert "blew up" in report["tasks"][0]["detail"]


def test_the_run_never_touches_the_operators_home_or_port(tmp_path):
    """Separate HOME, separate worktree root, separate port. A nightly run that
    shares any of the three with the operator's day instance can corrupt the
    queue they are looking at."""
    from no_human import config as cfg

    home = tmp_path / "home"
    run_funnel_eval(home, tmp_path / "out",
                    backend_factory=lambda t: _TierBackend(t.name),
                    reviewer=_PassReviewer(), corpus=_four_tier_corpus()[:1])

    report = json.loads(next((tmp_path / "out").glob("nightly-*.json")).read_text(encoding="utf-8"))
    inst = report["instance"]
    for key in ("db_path", "worktree_root", "workdir"):
        p = Path(inst[key]).resolve()
        assert p.is_relative_to(home.resolve()), f"{key} escaped the temp HOME: {p}"
        assert not p.is_relative_to(cfg.NO_HUMAN_HOME.resolve()), key
    assert inst["port"] != cfg.DEFAULT_CONFIG["server"]["port"]


@pytest.mark.slow
def test_three_tiers_pass_one_stalls_and_the_night_exits_one(tmp_path):
    """The headline case: a fake backend completes t1-t3 and hangs on t4. The
    hang must be killed and RECORDED with its stage, the other three must be
    unaffected, and the night must be red."""
    out = tmp_path / "out"
    rc = run_funnel_eval(tmp_path / "home", out,
                         backend_factory=lambda t: _TierBackend(t.name),
                         reviewer=_PassReviewer(), corpus=_four_tier_corpus())

    assert rc == 1
    report = json.loads(next(out.glob("nightly-*.json")).read_text(encoding="utf-8"))
    by_name = {t["task"]: t for t in report["tasks"]}
    assert [n for n, t in by_name.items() if t["passed"]] == [
        "t1_docs_oneliner", "t2_small_fix", "t3_small_feature"]
    stalled = by_name["t4_cross_file"]
    assert stalled["passed"] is False
    assert stalled["stage"] == "wall_clock_kill", stalled
    assert "max_wall_seconds" in " ".join(stalled["failures"])
    assert report["passed"] == 3 and report["failed"] == 1

    summary = (out / "SUMMARY.md").read_text(encoding="utf-8")
    assert "t4_cross_file" in summary and "wall_clock_kill" in summary
    assert "3 passed" in summary and "1 failed" in summary


@pytest.mark.slow
def test_the_holdout_decides_quality_not_the_reviewer(tmp_path):
    """A reviewer scripted to PASS everything cannot make a tier green: t2's
    quality comes from its held-out test and nothing else."""
    out = tmp_path / "out"
    corpus = [t for t in _four_tier_corpus() if t.name == "t2_small_fix"]
    run_funnel_eval(tmp_path / "home", out,
                    backend_factory=lambda t: _TierBackend("nothing-doing"),
                    reviewer=_PassReviewer(), corpus=corpus)

    rec = json.loads(next(out.glob("nightly-*.json")).read_text(encoding="utf-8"))["tasks"][0]
    assert rec["review_passed"] is True, "the reviewer did pass it"
    assert rec["quality"] == "holdout_red"
    assert rec["passed"] is False


def test_the_dated_report_is_named_for_the_night_it_ran(tmp_path):
    run_funnel_eval(tmp_path / "home", tmp_path / "out",
                    backend_factory=lambda t: _TierBackend(t.name),
                    reviewer=_PassReviewer(), corpus=_four_tier_corpus()[:1])
    names = [p.name for p in (tmp_path / "out").glob("nightly-*.json")]
    assert len(names) == 1
    assert re.fullmatch(r"nightly-\d{4}-\d{2}-\d{2}\.json", names[0]), names


def test_the_default_budget_is_the_corpus_ceiling():
    from no_human.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["eval"]["nightly_budget_tokens"] \
        == corpus_ceiling_tokens()


# --------------------------------------------------------------------------- #
# C4 — the ratchet                                                             #
# --------------------------------------------------------------------------- #

BASE = {
    "unseeded": False,
    "tasks": [{"task": "t1_docs_oneliner", "passed": True, "cost": 200_000},
              {"task": "t2_small_fix", "passed": True, "cost": 1_000_000}],
}


def test_identical_to_baseline_holds_the_ratchet():
    ok, lines = compare_to_baseline(
        [{"task": "t1_docs_oneliner", "passed": True, "cost": 200_000},
         {"task": "t2_small_fix", "passed": True, "cost": 1_000_000}], BASE)
    assert ok is True, lines


def test_a_pass_turning_into_a_fail_breaks_the_ratchet_and_names_the_task():
    ok, lines = compare_to_baseline(
        [{"task": "t1_docs_oneliner", "passed": True, "cost": 200_000},
         {"task": "t2_small_fix", "passed": False, "cost": 900_000}], BASE)
    assert ok is False
    assert any("t2_small_fix" in ln and "REGRESSION" in ln for ln in lines), lines


def test_a_cost_band_exceeded_by_more_than_a_quarter_breaks_it_with_numbers():
    ok, lines = compare_to_baseline(
        [{"task": "t1_docs_oneliner", "passed": True, "cost": 200_000},
         {"task": "t2_small_fix", "passed": True, "cost": 1_300_000}], BASE)
    assert ok is False
    hit = [ln for ln in lines if "t2_small_fix" in ln and "COST" in ln]
    assert hit, lines
    assert "1,300,000" in hit[0] and "1,250,000" in hit[0], hit[0]


def test_a_quarter_over_is_inside_the_band():
    ok, _ = compare_to_baseline(
        [{"task": "t1_docs_oneliner", "passed": True, "cost": 200_000},
         {"task": "t2_small_fix", "passed": True, "cost": 1_250_000}], BASE)
    assert ok is True


def test_a_better_run_does_not_auto_tighten_the_baseline(tmp_path):
    """A cheap night is a nice night, not a new floor. Auto-tightening turns
    one lucky run into a ratchet nobody can pass, and it is exactly how a
    measurement instrument becomes a target."""
    from no_human.eval import funnel_eval

    path = Path(funnel_eval.BASELINE_PATH)
    before = path.read_bytes()
    ok, lines = compare_to_baseline(
        [{"task": "t1_docs_oneliner", "passed": True, "cost": 1},
         {"task": "t2_small_fix", "passed": True, "cost": 1}], BASE)
    assert ok is True
    assert any("improved" in ln.lower() for ln in lines), lines
    assert path.read_bytes() == before, "the baseline file was rewritten"


def test_an_unseeded_baseline_reports_only_and_says_so():
    ok, lines = compare_to_baseline(
        [{"task": "t2_small_fix", "passed": False, "cost": 9_000_000}],
        {"unseeded": True, "tasks": []})
    assert ok is True, "an unseeded baseline cannot ratchet anything"
    assert any("unseeded" in ln.lower() and "report only" in ln.lower()
               for ln in lines), lines


def test_the_shipped_baseline_is_seeded_and_keeps_its_refresh_doctrine():
    """Seeded 2026-08-10 from the second real run (5/5 PASS on the post-
    incident tip), by a human who read it — the transition the unseeded-
    placeholder version of this test existed to guard. What must survive the
    seed: the refresh doctrine stays in the file, and the seed carries real
    rows (the tier-by-tier shape is pinned by
    test_the_seeded_baseline_covers_every_corpus_tier_from_a_passing_run)."""
    from no_human.eval import funnel_eval
    data = json.loads(Path(funnel_eval.BASELINE_PATH).read_text(encoding="utf-8"))
    assert data["unseeded"] is False
    assert data["tasks"] != []
    assert "_how_to_refresh" in data


def test_a_red_night_still_reports_the_ratchet_line_in_the_summary(
        tmp_path, monkeypatch):
    # Pins the UNSEEDED-baseline report line forever, decoupled from the
    # shipped baseline.json (which is seeded now): reading the shipped file
    # made this test's meaning change whenever that data file did.
    from no_human.eval import funnel_eval
    unseeded = tmp_path / "baseline.json"
    unseeded.write_text(json.dumps(
        {"unseeded": True, "tasks": [], "_how_to_refresh": "test fixture"}))
    monkeypatch.setattr(funnel_eval, "BASELINE_PATH", unseeded)
    run_funnel_eval(tmp_path / "home", tmp_path / "out",
                    backend_factory=lambda t: _TierBackend(t.name),
                    reviewer=_PassReviewer(), corpus=_four_tier_corpus()[:1])
    summary = (tmp_path / "out" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "unseeded" in summary.lower() and "report only" in summary.lower()


# --------------------------------------------------------------------------- #
# Review cure (2026-08-10) — the two blockers and what they were hiding        #
# --------------------------------------------------------------------------- #

class _ExplodingFactory:
    """Fails the test loudly if the runner reaches a backend at all."""

    def __call__(self, task):
        raise AssertionError(f"the runner must not have started {task.name}")


def test_an_empty_corpus_is_a_refusal_not_a_green_night(tmp_path):
    """`0 passed, 0 failed, exit 0` is the "empty is not zero" defect: a night
    that ran nothing reported a clean bill of health. An empty corpus is the
    instrument being broken, and a broken instrument is RED."""
    rc = run_funnel_eval(tmp_path / "home", tmp_path / "out",
                         backend_factory=_ExplodingFactory(),
                         reviewer=_PassReviewer(), corpus=[])

    assert rc == 1
    summary = (tmp_path / "out" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "REFUSED" in summary and "empty" in summary.lower(), summary


def test_a_corpus_missing_a_tier_is_a_refusal_that_names_the_missing_tier(
        tmp_path, monkeypatch):
    """The reachable version: `eval/funnel_corpus/` exists but has lost tiers,
    so `load_corpus()` returns fewer than five and the night silently measures
    less than it claims to. Fail closed, and say which ones are gone."""
    from no_human.eval import funnel_eval as fe

    monkeypatch.setattr(
        fe, "load_corpus",
        lambda: [t for t in load_corpus() if t.name == "t1_docs_oneliner"])
    rc = run_funnel_eval(tmp_path / "home", tmp_path / "out",
                         backend_factory=_ExplodingFactory(),
                         reviewer=_PassReviewer())

    assert rc == 1
    summary = (tmp_path / "out" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "REFUSED" in summary
    for missing in ("t2_small_fix", "t3_small_feature", "t4_cross_file",
                    "t5_test_first"):
        assert missing in summary, summary
    assert "t1_docs_oneliner" not in summary.split("REFUSED")[1].split("|")[0]


def test_the_shipped_path_wires_a_real_reviewer(tmp_path, monkeypatch):
    """The gate is the product. `main()` used to leave `reviewer=None`, which
    with `reviewer.allow_advisory` false raises ReviewerUnavailable AFTER the
    coder attempt has been paid for — every tier red, every night, for a
    reason that reads like a product regression. The default path must build
    the same reviewer `nh bench` builds."""
    from no_human.review.reviewer import AdversarialReviewer

    seen = {}
    real_init = AdversarialReviewer.__init__

    def spy(self, **kw):
        seen.update(kw)
        real_init(self, **kw)

    monkeypatch.setattr(AdversarialReviewer, "__init__", spy)
    corpus = [t for t in _four_tier_corpus() if t.name == "t1_docs_oneliner"]
    run_funnel_eval(tmp_path / "home", tmp_path / "out",
                    backend_factory=lambda t: _TierBackend(t.name),
                    corpus=corpus)

    assert seen.get("model") == "claude-opus-4-8", seen


def test_the_reviewer_model_comes_from_the_runs_own_instance_config(tmp_path):
    """Not from the raw `config` argument, and not from DEFAULT_CONFIG. Same
    value today, divergent by construction: the instance runs on
    `_instance_config(home, config)`, so a reviewer resolved anywhere else can
    silently sit on a different model from the one the run reports."""
    made = []

    class _SpyReviewer:
        def __init__(self, *, model):
            made.append(model)

        @classmethod
        def from_config(cls, data, **kw):
            # The production factory (`AdversarialReviewer.from_config`) reads
            # the model out of the config dict it is HANDED. What this test
            # guards is unchanged and is the whole point: WHICH dict arrives.
            # Hand it DEFAULT_CONFIG or the raw `config=` argument instead of
            # the instance config and the pinned name below never appears.
            return cls(model=(data.get("llm") or {}).get("review_model"))

        async def review(self, *a, **kw):
            raise AssertionError("not reached")

    # The pin goes into the HOME's own config.yaml and `config=None` — the
    # SHIPPED shape (main() never passes config). Passing the pinned dict as
    # `config=` is the one input where the cured and the pre-cure spelling
    # agree (round-4 review: the reverted body passed that version), so it
    # would guard nothing.
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "llm:\n  review_model: claude-opus-5-pinned-by-this-test\n")
    with __import__("unittest.mock", fromlist=["patch"]).patch(
            "no_human.review.reviewer.AdversarialReviewer", _SpyReviewer):
        run_funnel_eval(home, tmp_path / "out",
                        backend_factory=lambda t: _TierBackend(t.name),
                        corpus=[], config=None)

    assert made == ["claude-opus-5-pinned-by-this-test"], made


class _RecordingBackend:
    """Records the kwargs the shipped default constructs a coder with, and
    then behaves like a working coder so the tier really completes."""

    made: list[dict] = []

    def __init__(self, **kwargs):
        self.model = kwargs.get("model")
        _RecordingBackend.made.append(kwargs)

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        from no_human.agent.claude_backend import AgentResult
        # Tolerant: the Orchestrator builds its own advisory backends through
        # the same class, and those run against directories with no README.
        readme = Path(cwd) / "README.md"
        if readme.exists():
            readme.write_text(readme.read_text(encoding="utf-8").replace(
                "at most 100 items", "at most 200 items"))
        return AgentResult(final_text="done", num_turns=2, is_error=False,
                           tokens_used=1200, session_id="s",
                           stop_reason="end_turn")


@pytest.mark.slow
def test_the_shipped_path_constructs_a_coder_backend_that_actually_works(
        tmp_path, monkeypatch):
    """`backend_factory` OMITTED — the path every night takes and no test ever
    exercised. `ClaudeBackend.__init__` takes `model` keyword-only with no
    default, so the obvious `ClaudeBackend()` is a TypeError raised BEFORE the
    coder starts: five crashed tiers, exit 1, every night, for a reason that
    is not a regression."""
    # Decouple from the shipped (now seeded) baseline: this one-tier fixture
    # run would otherwise report every seeded tier missing and exit 1.
    from no_human.eval import funnel_eval
    unseeded = tmp_path / "baseline.json"
    unseeded.write_text(json.dumps(
        {"unseeded": True, "tasks": [], "_how_to_refresh": "test fixture"}))
    monkeypatch.setattr(funnel_eval, "BASELINE_PATH", unseeded)
    _RecordingBackend.made.clear()
    monkeypatch.setattr("no_human.agent.claude_backend.ClaudeBackend",
                        _RecordingBackend)
    corpus = [t for t in _four_tier_corpus() if t.name == "t1_docs_oneliner"]

    rc = run_funnel_eval(tmp_path / "home", tmp_path / "out",
                         reviewer=_PassReviewer(), corpus=corpus)

    # The Orchestrator builds its OWN advisory backends (intake evaluator,
    # distillation) through the same class, so filter to the coder — the one
    # construction that is not readonly. That those others exist and are
    # haiku-tier readonly is the product's business, not this runner's.
    coder = [kw for kw in _RecordingBackend.made if not kw.get("readonly")]
    assert len(coder) == 1, _RecordingBackend.made
    kw = coder[0]
    # The implementer tier, from the run's own config — not the reviewer's.
    assert kw["model"] == "claude-sonnet-5", kw
    # The two guards `nh bench` arms and this must arm too: the PreToolUse
    # safety boundary, and the never-push rule (constraint #2). Their VALUES
    # come from the instance config, so a hardened nightly config is honoured.
    assert kw["forbidden_paths"] == [".env", "secrets/", "*.key", "*.pem"], kw
    assert kw["never_push_to"] == ["main", "master", "release/*"], kw
    # Nothing else: permission_mode/readonly defaults are already correct and
    # the hooks are the Orchestrator's to wire, exactly as in `nh bench`.
    assert set(kw) == {"model", "forbidden_paths", "never_push_to"}, kw
    # And it is a WORKING backend, not just a well-formed constructor call.
    assert rc == 0, (tmp_path / "out" / "SUMMARY.md").read_text(encoding="utf-8")


@pytest.mark.slow
def test_a_tier_cannot_pass_with_the_review_gate_absent(tmp_path):
    """The repair that would have faked this green: setting
    `reviewer.allow_advisory` so a SKIPPED gate returns passed=True. With no
    reviewer, a tier must not pass — the criterion is a real review, not the
    absence of an objection."""
    out = tmp_path / "out"
    corpus = [t for t in _four_tier_corpus() if t.name == "t1_docs_oneliner"]
    rc = run_funnel_eval(tmp_path / "home", out,
                         backend_factory=lambda t: _TierBackend(t.name),
                         reviewer=None, corpus=corpus)

    assert rc == 1
    rec = json.loads(next(out.glob("nightly-*.json")).read_text(encoding="utf-8"))["tasks"][0]
    assert rec["review_passed"] is False, rec
    assert rec["passed"] is False
    assert any(f.startswith("review_passed:") for f in rec["failures"]), rec


@pytest.mark.slow
def test_each_tier_carries_its_own_ceiling_as_a_real_per_task_budget(tmp_path):
    """The static pre-check compares ceilings pinned EQUAL to their own sum, so
    it can never fire on its own. What actually stops a runaway tier is
    `bounds.lifetime_tokens`, which defaults to 4M for EVERY task — five tiers
    of that is a 20M night, not the 10.9M the doc calls authorised. File each
    tier's ceiling as the task's own cap, stamped `budget_unit: weighted` so
    the cutover guard does not read it as raw and convert it down 5x."""
    import asyncio

    from no_human.core.db import Store
    from no_human.core.pricing import BUDGET_UNIT_KEY, WEIGHTED_UNIT

    home = tmp_path / "home"
    corpus = [t for t in _four_tier_corpus() if t.name == "t2_small_fix"]
    run_funnel_eval(home, tmp_path / "out",
                    backend_factory=lambda t: _TierBackend(t.name),
                    reviewer=_PassReviewer(), corpus=corpus)

    async def _read():
        store = await Store(home / "no_human.db").connect()
        try:
            return [t.config for t in await store.list_tasks()]
        finally:
            await store.close()

    configs = asyncio.run(_read())
    assert len(configs) == 1, configs
    assert configs[0]["lifetime_tokens"] == corpus[0].criteria.max_weighted_tokens
    assert configs[0][BUDGET_UNIT_KEY] == WEIGHTED_UNIT, (
        "an unstamped cap is read as RAW and converted down ~5x")


def test_a_wedged_holdout_is_killed_by_process_group(tmp_path, monkeypatch):
    """`_holdout_ok`'s timeout path had no test. A plain `proc.kill()` reaps
    the shell and leaves the real work running; the marker file below is
    written by a GRANDCHILD, so it only stays absent if the whole group died."""
    import dataclasses
    import time

    from no_human.eval import funnel_eval as fe

    marker = tmp_path / "grandchild-survived"
    # The grandchild touches the marker at +3s and the kill lands at +1s, so
    # checking at ~+5s is a REAL test of the group kill: a surviving grandchild
    # has written the file by then. The first version slept 30s before
    # touching, which made `not marker.exists()` true whether the group died or
    # not — the timing bound below was doing all the work and the marker was
    # decoration. Both are asserted now, and they catch different failures: the
    # bound catches "never killed at all", the marker catches "only the direct
    # child was killed".
    script = f"sleep 3; touch {marker}"
    task = dataclasses.replace(
        load_corpus()[0],
        holdout_cmd=["/bin/sh", "-c", f"/bin/sh -c '{script}' & wait"])
    monkeypatch.setattr(fe, "HOLDOUT_TIMEOUT_S", 1)

    t0 = time.monotonic()
    assert fe._holdout_ok(task, tmp_path) is False, "a wedged holdout is RED"
    assert time.monotonic() - t0 < 3, "it must not wait out the sleep"
    time.sleep(5)
    assert not marker.exists(), (
        "the grandchild outlived the kill — the process GROUP was not killed")


def test_the_holdout_env_leaves_no_bytecode_in_the_tree(tmp_path):
    """A holdout run used to drop `__pycache__` into the corpus checkout, which
    then shows up as untracked noise in every subsequent status."""
    from no_human.eval import funnel_eval as fe

    captured = {}

    class _Popen:
        def __init__(self, *a, **kw):
            captured.update(kw.get("env") or {})
            self.returncode = 0

        def communicate(self, timeout=None):
            return ("", "")

    import subprocess as sp
    real = sp.Popen
    sp.Popen = _Popen
    try:
        fe._holdout_ok(load_corpus()[1], tmp_path)
    finally:
        sp.Popen = real
    assert captured.get("PYTHONDONTWRITEBYTECODE") == "1", captured


def test_the_seeded_baseline_covers_every_corpus_tier_from_a_passing_run():
    """A seed that omits a tier silently disables the ratchet for it, and a
    seed copied from a FAILING or empty run arms the ratchet with a floor
    nobody measured. Pins the real seeded file's shape: every EXPECTED_TIERS
    member present, seeded from a pass, with a real cost and wall."""
    import json as _json

    from no_human.eval.funnel_corpus import CORPUS_DIR, EXPECTED_TIERS

    baseline = _json.loads((CORPUS_DIR / "baseline.json").read_text(encoding="utf-8"))
    assert baseline.get("unseeded") is False, "the ratchet is not armed"
    assert baseline.get("recorded"), "a seed must say when it was measured"
    assert baseline.get("product_commit"), "a seed must say what it measured"
    rows = {r["task"]: r for r in baseline.get("tasks", [])}
    assert set(rows) == set(EXPECTED_TIERS), (
        f"baseline covers {sorted(rows)}, corpus has {sorted(EXPECTED_TIERS)}")
    for tier, row in rows.items():
        assert row["passed"] is True, f"{tier} seeded from a FAILING run"
        assert row["cost"] > 0, f"{tier} seeded with an empty cost"
        assert row["wall_seconds"] > 0, f"{tier} seeded with an empty wall"
