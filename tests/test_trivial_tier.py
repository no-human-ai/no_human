"""Trivial-tier fast path: ceremony scales with change risk, gates do not.

Motivating trace (2026-08-09, task b3e8813f): deleting one struck phrase from
a single markdown file cost 35+ minutes — two intake scoping questions, a
9-turn claude-opus-5 planning pass, 9 skills loaded, then a multi-stage Opus
review. The complexity gate computed "tier simple (no signals)" and nothing
downstream consumed the verdict.

These tests pin BOTH halves: what gets cheaper (scoping questions, planner,
skills, review turns) and what never does (the review still runs; anything that leaves the
predicate escalates back to full ceremony before the review).
"""

import pytest

from no_human.config import load_config
from no_human.core.complexity import (
    compute_tier,
    review_must_be_unbounded,
    is_trivial,
    named_paths,
    path_tokens,
    trivial_enabled,
    trivial_paths,
)
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.intake import evaluator as ev
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import (
    _REVIEW_TURNS,
    _TRIVIAL_REVIEW_TURNS,
    AdversarialReviewer,
)


class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


def _orch(store, tmp_path, events, cfg_overlay=None):
    cfg = load_config(tmp_path / "config.yaml")
    if cfg_overlay:
        cfg.data.update(cfg_overlay)
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)


def _git_init(path):
    """Make `path` a real, minimal git repo.

    `orch._run_reviewer` snapshots `repo_path` via
    `reviewer_worktree.snapshot()` unconditionally, for every mode, before the
    reviewer ever runs — it needs a resolvable `git rev-parse HEAD`. Production
    `repo_path` is always a real checkout; a bare `tmp_path` is not, so these
    tests fail closed with `WorktreeCheckFailed` before `_changed_paths` (which
    every one of them monkeypatches) is ever consulted. One empty commit is
    enough — no test here reads real git history.
    """
    import subprocess

    def run(*a):
        return subprocess.run(["git", *a], cwd=path, check=True,
                              capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    run("commit", "--allow-empty", "-qm", "base")


def _task(**kw):
    defaults = dict(id="aaa", source="test", title="t",
                    status=TaskStatus.PENDING, acceptance_criteria=[])
    defaults.update(kw)
    return Task(**defaults)


# ------------------------------------------------------------- predicate ----

@pytest.mark.parametrize("paths", [
    ["notes/positioning.md"],
    ["docs/adapters.md", "README.md"],
    ["./docs/adapters.md"],
    ["CHANGELOG.rst", "NOTES.txt"],
])
def test_prose_file_sets_are_trivial(paths):
    assert trivial_paths(paths) is True


@pytest.mark.parametrize("paths, why", [
    (["src/no_human/core/orchestrator.py"], "code"),
    (["src/no_human/README.md"], "prose that SHIPS in the wheel"),
    ([".claude/skills/verify/SKILL.md"], "prose an agent runtime executes"),
    (["tests/test_docs.py"], "a test"),
    (["web/src/App.tsx"], "frontend code"),
    (["desktop/main.js"], "desktop code"),
    (["scripts/export_guard.py"], "a script"),
    (["docs/a.md", "docs/b.md", "docs/c.md"], "more than 2 files"),
    (["docs/a.md", "src/x.py"], "one code file among prose"),
    (["pyproject.toml"], "config, not prose"),
    (["Makefile"], "no recognised suffix"),
    ([], "nothing named at all"),
])
def test_non_prose_or_oversized_file_sets_are_not_trivial(paths, why):
    assert trivial_paths(paths) is False, why


def test_path_tokens_ignores_prose_punctuation():
    """The pre-plan reader runs over free text: `e.g.`, `1.5x` and a dotted
    module name are not files, and must not be counted as either evidence or
    veto."""
    text = ("remove a struck phrase from notes/positioning.md, "
            "e.g. the one at 1.5x scale in no_human.core, twice: "
            "notes/positioning.md")
    assert path_tokens(text) == ["notes/positioning.md"]


def test_named_paths_prefers_the_plan_over_the_ticket():
    """The plan is the more authoritative statement, and the one the
    post-plan escalation check reads."""
    t = _task(title="tidy docs/intro.md")
    assert named_paths(t) == ["docs/intro.md"]
    t.context = {"spec": {"files_to_change": ["`src/no_human/x.py` — the fix"]}}
    assert named_paths(t) == ["src/no_human/x.py"]
    assert compute_tier(t)[0] != "trivial"


def test_ambiguity_falls_back_to_simple():
    """A ticket that names nothing, or names something unclassifiable, must
    NOT be fast-pathed — the fast path requires positive evidence."""
    assert compute_tier(_task(title="make the board nicer"))[0] == "simple"
    assert compute_tier(_task(title="update the Makefile"))[0] == "simple"


def test_config_off_switch(tmp_path):
    assert trivial_enabled(load_config(tmp_path / "c.yaml").data) is True
    assert trivial_enabled({"pipeline": {"trivial_tier": {"enabled": False}}}) is False
    # A hand-edited `pipeline:` with its body commented out deep-merges to None.
    assert trivial_enabled({"pipeline": None}) is True
    assert trivial_enabled(None) is True


# --------------------------------------------------- classification wiring ---

async def test_classify_records_the_tier_and_says_what_it_skips(
        store, tmp_path):
    t = Task.new("Drop a struck phrase from notes/positioning.md",
                 repo_path="/r")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    assert await orch._classify_tier(t) == "trivial"

    got = await store.get_task(t.id)
    assert got.context["complexity_tier"] == "trivial"
    fp = [e for e in events if e.get("kind") == "fast_path"]
    assert len(fp) == 1
    # The honesty requirement: an operator reads WHAT was skipped, not just
    # that something was.
    for skipped in ("scoping questions", "utility-model planner",
                    "skill discovery",
                    "bounded"):
        assert skipped in fp[0]["text"]
    assert "tamper guard" in fp[0]["text"]


async def test_off_switch_restores_full_ceremony(store, tmp_path):
    t = Task.new("Drop a struck phrase from notes/positioning.md",
                 repo_path="/r")
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events,
                 cfg_overlay={"pipeline": {"trivial_tier": {"enabled": False}}})
    assert await orch._classify_tier(t) == "simple"
    assert not is_trivial(await store.get_task(t.id))
    assert not [e for e in events if e.get("kind") == "fast_path"]
    assert any("disabled" in e.get("text", "") for e in events)


# ---------------------------------------------------------- stage effects ----

async def test_trivial_tier_skips_the_intake_grill(store, tmp_path, monkeypatch):
    called = []

    async def _fake_grill(*a, **k):  # pragma: no cover
        called.append(1)
        return []
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    await _orch(store, tmp_path, events)._run_intake_grill(t)

    assert called == []
    assert any(e.get("kind") == "intake_grill" and "skipped" in e["text"]
               for e in events)


async def test_a_non_trivial_task_still_grills(store, tmp_path, monkeypatch):
    """The guard must be the tier, not a blanket skip."""
    called = []

    async def _fake_grill(*a, **k):
        called.append(1)
        return []
    monkeypatch.setattr(ev, "grill_spec", _fake_grill)

    t = Task.new("t", repo_path="/r")
    t.context = {"complexity_tier": "simple"}
    await store.create_task(t)
    await _orch(store, tmp_path, [])._run_intake_grill(t)
    assert called == [1]


async def test_trivial_tier_plans_on_the_utility_model_in_two_turns(
        store, tmp_path, monkeypatch):
    """The four-tier rule reserves the utility tier for advisory work: a plan is
    advisory (the coder may depart from it) and the reviewer still gates."""
    seen = {}

    class _Planner:
        def __init__(self, *a, model=None, **k):
            seen["model"] = model

        async def run(self, prompt, *, cwd=None, max_turns=None, **k):
            from no_human.agent.claude_backend import AgentResult
            seen["max_turns"] = max_turns
            return AgentResult(final_text="## APPROACH\ndelete the phrase",
                               num_turns=1, is_error=False, tokens_used=1,
                               session_id="s", stop_reason="end_turn")

    import no_human.core.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "ClaudeBackend", _Planner)

    t = Task.new("Drop a phrase from notes/positioning.md",
                 repo_path=str(tmp_path))
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)

    class _Repo:
        path = tmp_path

    plan = await orch._generate_plan(t, _Repo())
    assert plan
    assert seen["model"] == "claude-haiku-4-5"
    assert seen["max_turns"] == 2
    assert any("trivial tier" in e.get("text", "") for e in events
               if e.get("kind") == "planning")


async def test_moa_fan_out_never_fires_on_the_trivial_tier(
        store, tmp_path, monkeypatch):
    """Even with the documented `min_signals: 0` override, which otherwise
    means "fan out on everything"."""
    fanned = []

    async def _moa(*a, **k):  # pragma: no cover
        fanned.append(1)
        return "plan"

    class _Planner:
        def __init__(self, *a, **k):
            pass

        async def run(self, prompt, **k):
            from no_human.agent.claude_backend import AgentResult
            return AgentResult(final_text="## APPROACH\nx", num_turns=1,
                               is_error=False, tokens_used=1, session_id="s",
                               stop_reason="end_turn")

    import no_human.core.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "ClaudeBackend", _Planner)

    t = Task.new("Drop a phrase from notes/positioning.md",
                 repo_path=str(tmp_path))
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    orch = _orch(store, tmp_path, [], cfg_overlay={
        "llm": {"moa_planning": {"enabled": True, "min_signals": 0}}})
    monkeypatch.setattr(orch, "_generate_plan_moa", _moa)

    class _Repo:
        path = tmp_path

    await orch._generate_plan(t, _Repo())
    assert fanned == []


def test_reviewer_turn_budget_is_bounded_not_removed():
    t = _task()
    t.context = {"complexity_tier": "trivial"}
    assert AdversarialReviewer._tier_review_turns(t) == _TRIVIAL_REVIEW_TURNS
    assert 0 < _TRIVIAL_REVIEW_TURNS < _REVIEW_TURNS   # bounded, still a review
    t.context = {"complexity_tier": "simple"}
    assert AdversarialReviewer._tier_review_turns(t) == _REVIEW_TURNS
    assert AdversarialReviewer._tier_review_turns(_task()) == _REVIEW_TURNS


# ------------------------------------------------------------ escalation ----

async def test_a_plan_that_leaves_the_predicate_revokes_the_tier(
        store, tmp_path):
    """Checkpoint 1: the plan names the file set for the first time."""
    t = Task.new("Drop a phrase from notes/positioning.md",
                 repo_path="/r")
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)

    await orch._persist_plan(t, "## FILES TO CHANGE/CREATE\n- src/no_human/core/x.py\n")

    got = await store.get_task(t.id)
    assert got.context["complexity_tier"] == "simple"
    assert any(e.get("kind") == "fast_path" and "revoked" in e["text"]
               for e in events)


async def test_a_plan_that_holds_keeps_the_tier(store, tmp_path):
    t = Task.new("Drop a phrase from notes/positioning.md",
                 repo_path="/r")
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    orch = _orch(store, tmp_path, [])
    await orch._persist_plan(t, "## FILES TO CHANGE/CREATE\n- notes/positioning.md\n")
    assert is_trivial(await store.get_task(t.id))


async def test_a_diff_that_leaves_the_predicate_revokes_before_review(
        store, tmp_path, monkeypatch):
    """Checkpoint 2, the one that protects the gate: the coder's ACTUAL diff.
    A task that talked its way onto the fast path and then edited code gets the
    full review, not a bounded one."""
    seen = {}

    class _Reviewer:
        async def review(self, task, **kwargs):
            seen["tier"] = (task.context or {}).get("complexity_tier")
            return "decision"

    t = Task.new("Drop a phrase from notes/positioning.md",
                 repo_path="/r")
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    orch.reviewer = _Reviewer()
    monkeypatch.setattr("no_human.review.reviewer._changed_paths",
                        lambda *a, **k: ["src/no_human/core/orchestrator.py"])
    _git_init(tmp_path)

    await orch._run_reviewer(t, repo_path=tmp_path)

    assert seen["tier"] == "simple"          # reviewer sees the raised tier
    assert AdversarialReviewer._tier_review_turns(t) == _REVIEW_TURNS
    assert any(e.get("kind") == "fast_path" and "revoked" in e["text"]
               for e in events)


async def test_an_unreadable_diff_revokes_the_tier(store, tmp_path, monkeypatch):
    """Fail-safe: only a positive predicate keeps the bound."""
    def _boom(*a, **k):
        raise OSError("no git here")

    class _Reviewer:
        async def review(self, task, **kwargs):
            return "decision"

    t = Task.new("Drop a phrase from notes/positioning.md",
                 repo_path="/r")
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    orch = _orch(store, tmp_path, [])
    orch.reviewer = _Reviewer()
    monkeypatch.setattr("no_human.review.reviewer._changed_paths", _boom)
    _git_init(tmp_path)

    await orch._run_reviewer(t, repo_path=tmp_path)
    assert not is_trivial(t)


async def test_a_prose_diff_keeps_the_bounded_review(store, tmp_path,
                                                     monkeypatch):
    class _Reviewer:
        async def review(self, task, **kwargs):
            return "decision"

    t = Task.new("Drop a phrase from notes/positioning.md",
                 repo_path="/r")
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    orch = _orch(store, tmp_path, [])
    orch.reviewer = _Reviewer()
    monkeypatch.setattr("no_human.review.reviewer._changed_paths",
                        lambda *a, **k: ["notes/positioning.md"])
    _git_init(tmp_path)

    await orch._run_reviewer(t, repo_path=tmp_path)
    assert is_trivial(t)
    assert AdversarialReviewer._tier_review_turns(t) == _TRIVIAL_REVIEW_TURNS


def test_the_tier_summary_shows_the_reduced_ceremony():
    """`nh task tier <id>` — an operator must be able to SEE the ceremony was
    reduced and that the gates were not."""
    from no_human.cli.commands import format_tier_summary

    out = format_tier_summary("trivial", [], predicted=False)
    assert "intake scoping questions: skipped" in out
    assert "utility model" in out
    assert "bounded single pass" in out
    assert "tamper guard" in out
    assert "escalates" in out
    assert ".agents/" in out          # R9: instruction edits keep the full review
    # ...and no other tier grows the section.
    assert "intake scoping" not in format_tier_summary("simple", [],
                                                     predicted=False)


# ------------------------------------------------ agent-instruction prose ----
#
# R9 forensics (2026-08-09): the motivating incident was a one-line edit to an
# agent DEFINITION under `.agents/`. Two things have to be
# true at once, and they are not the same thing: the tier decides how much
# CEREMONY a change is worth (the incident's 35 minutes), while trust in the
# result is the review gate's job. So the one-liner must reach the fast path,
# and the review of it must not be the bounded one.

def test_the_motivating_one_liner_under_dot_agents_is_trivial():
    """The trace this whole path exists for: `.agents/*.md` is prose nothing
    imports or ships, so it is admissible. `.agents/skills/**` is NOT — a
    SKILL.md is loaded into a live agent session."""
    assert trivial_paths([".agents/reviewer.md"]) is True
    assert trivial_paths([".agents/skills/verify/SKILL.md"]) is False
    t = _task(title="Remove the struck phrase from .agents/reviewer.md",
              description="Delete the one struck phrase.",
              acceptance_criteria=["the phrase is gone"])
    assert compute_tier(t)[0] == "trivial"


def test_review_must_be_unbounded_covers_instructions_and_gate_control():
    """R9 review F1/F2. Two families whose one-line diff is high-consequence:
    agent instructions (matched by BASENAME at any depth — `docs/CLAUDE.md` is
    the same hazard as the root one) and the repo's own gate-control data."""
    assert review_must_be_unbounded([".agents/reviewer.md"]) is True
    assert review_must_be_unbounded(["./.agents/x.md"]) is True
    assert review_must_be_unbounded(["docs/adapters.md", ".agents/x.md"]) is True
    # F1: the files holding "The agent never merges".
    assert review_must_be_unbounded(["CLAUDE.md"]) is True
    assert review_must_be_unbounded(["docs/CLAUDE.md"]) is True
    assert review_must_be_unbounded(["AGENTS.md"]) is True
    # F2: `.txt` to the suffix test, RULES to the gates that read them.
    assert review_must_be_unbounded(["EXPORT_CLASSIFICATION.txt"]) is True
    assert review_must_be_unbounded(["RELEASE_MANIFEST.txt"]) is True
    assert review_must_be_unbounded(["docs/adapters.md", "README.md"]) is False
    assert review_must_be_unbounded(["agents/x.md"]) is False  # not the dotted root
    assert review_must_be_unbounded([]) is False


def test_the_instruction_names_come_from_the_repos_own_list():
    """Not a second hand-maintained copy: a new convention file added to
    `_REPO_INSTRUCTION_FILES` is covered here without touching this module."""
    from no_human.core.orchestrator import Orchestrator
    for rel in Orchestrator._REPO_INSTRUCTION_FILES:
        assert review_must_be_unbounded([f"docs/{rel.rsplit('/', 1)[-1]}"]) is True


def test_testdata_is_executed_not_prose():
    """F2: `testdata/**` is asserted on, exactly like `tests/**`."""
    assert trivial_paths(["testdata/machinery_strings.txt"]) is False


async def test_an_agent_instruction_diff_gets_the_FULL_review(
        store, tmp_path, monkeypatch):
    """The guard. A one-line edit to an agent definition is cheap to PLAN and
    expensive to get wrong: one sentence changes what an agent does on every
    future run. It stays admissible to the fast path (the ceremony above is
    still cut) but the adversarial review that reads the diff is the full one,
    not `_TRIVIAL_REVIEW_TURNS`."""
    seen = {}

    class _Reviewer:
        async def review(self, task, **kwargs):
            seen["tier"] = (task.context or {}).get("complexity_tier")
            return "decision"

    t = Task.new("Remove the struck phrase from .agents/reviewer.md",
                 repo_path="/r")
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    orch.reviewer = _Reviewer()
    monkeypatch.setattr("no_human.review.reviewer._changed_paths",
                        lambda *a, **k: [".agents/reviewer.md"])
    _git_init(tmp_path)

    await orch._run_reviewer(t, repo_path=tmp_path)

    assert seen["tier"] == "simple"
    assert AdversarialReviewer._tier_review_turns(t) == _REVIEW_TURNS
    assert any(e.get("kind") == "fast_path" and "agent instructions" in e["text"]
               for e in events), [e.get("text") for e in events]


# --------------------------------------- R3 (planner retry) × the fast path --

async def test_a_starved_trivial_planner_is_a_benign_skip_not_a_failure(
        store, tmp_path, monkeypatch):
    """R3 retries a turn-starved planner at DOUBLE the budget and, failing
    twice, tells the coder planning FAILED so it explores before editing.

    Neither half may fire on the fast path. The cap is not an accident to
    recover from — it is the tier's own `_TRIVIAL_PLAN_TURNS`, so doubling it
    undoes the cut; and running on the ticket alone for a ≤2-file prose edit is
    the tier's BET, not a failure, so `plan_unavailable` would buy the
    exploration pass back by another door. What survives from R3: the drop is
    stated out loud, never silent."""
    from no_human.agent.claude_backend import AgentResult

    budgets = []

    class _Planner:
        def __init__(self, *a, **k):
            pass

        async def run(self, prompt, *, cwd=None, max_turns=None, **k):
            budgets.append(max_turns)
            return AgentResult(
                final_text="Claude Code returned an error result: Reached "
                           "maximum number of turns (2)",
                num_turns=2, is_error=True, tokens_used=1, session_id="s",
                stop_reason="max_turns")

    import no_human.core.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "ClaudeBackend", _Planner)

    t = Task.new("Drop a phrase from notes/positioning.md",
                 repo_path=str(tmp_path))
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)

    class _Repo:
        path = tmp_path

    assert await orch._generate_plan(t, _Repo()) == ""
    assert budgets == [2], "R3's double-budget retry re-inflates the tier's cap"
    assert "plan_unavailable" not in (t.context or {})
    assert (await store.get_task(t.id)).context.get("plan_unavailable") is None
    texts = [e.get("text", "") for e in events if e.get("kind") == "planning"]
    assert any("benign" in x for x in texts), texts
    assert not any("planning FAILURE" in x or "no plan for the coder" in x
                   for x in texts), texts


async def test_a_starved_planner_off_the_fast_path_still_gets_R3(
        store, tmp_path, monkeypatch):
    """The control: R3 is untouched everywhere else."""
    from no_human.agent.claude_backend import AgentResult

    budgets = []

    class _Planner:
        def __init__(self, *a, **k):
            pass

        async def run(self, prompt, *, cwd=None, max_turns=None, **k):
            budgets.append(max_turns)
            return AgentResult(final_text="Reached maximum number of turns",
                               num_turns=1, is_error=True, tokens_used=1,
                               session_id="s", stop_reason="max_turns")

    import no_human.core.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "ClaudeBackend", _Planner)

    t = Task.new("Rework the scheduler", repo_path=str(tmp_path))
    t.context = {"complexity_tier": "simple"}
    await store.create_task(t)
    orch = _orch(store, tmp_path, [])

    class _Repo:
        path = tmp_path

    assert await orch._generate_plan(t, _Repo()) == ""
    assert budgets == [10, 20]
    assert "ran out of turns twice" in t.context["plan_unavailable"]


async def test_a_gate_control_diff_gets_the_FULL_review(store, tmp_path,
                                                        monkeypatch):
    """F2: a one-line drop->ship reclassification must not review itself under
    a 6-turn bound. It stays admissible (the ceremony above is still cut) and
    the review that reads the diff is the full one."""
    class _Reviewer:
        async def review(self, task, **kwargs):
            return "decision"

    t = Task.new("Reclassify one line in EXPORT_CLASSIFICATION.txt",
                 repo_path="/r")
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    orch.reviewer = _Reviewer()
    monkeypatch.setattr("no_human.review.reviewer._changed_paths",
                        lambda *a, **k: ["EXPORT_CLASSIFICATION.txt"])
    _git_init(tmp_path)

    await orch._run_reviewer(t, repo_path=tmp_path)
    assert not is_trivial(t)
    assert AdversarialReviewer._tier_review_turns(t) == _REVIEW_TURNS


def _seeded_repo(tmp_path):
    """A real git repo with a source module, a doc and an instruction file.

    Real git, never a monkeypatched `_changed_paths`: every defect this block
    pins lived in the SEAM between what git reports and what the checkpoint
    reads, and a stubbed path list would have asserted my own assumption
    instead of git's behaviour.
    """
    import subprocess

    repo = tmp_path / "repo"
    (repo / "src" / "no_human" / "core").mkdir(parents=True)
    (repo / "docs").mkdir()

    def run(*a):
        return subprocess.run(["git", *a], cwd=repo, check=True,
                              capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    # Long enough that git scores the move as R100 rather than an add+delete.
    (repo / "src/no_human/core/never_push.py").write_text(
        "GUARD = True\nNEVER_PUSH = ('main', 'master')\n" + "x = 1\n" * 40)
    (repo / "CLAUDE.md").write_text("The agent never merges.\n" + "rule\n" * 40)
    (repo / "docs/x.md").write_text("one\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    return repo, run


class _Reviewer:
    async def review(self, task, **kwargs):
        return "decision"


async def test_a_deletion_in_a_mixed_diff_revokes_the_tier(store, tmp_path):
    """F3: `_changed_paths` drops D entries (it feeds the reviewer FILES TO
    SHOW, and a deleted file has no text). Checkpoint 2 reused it, so a mixed
    diff — delete a source file, touch one doc — kept the 6-turn bound while
    the diff was nothing of the kind."""
    repo, run = _seeded_repo(tmp_path)
    (repo / "src/no_human/core/never_push.py").unlink()
    (repo / "docs/x.md").write_text("two\n")
    run("add", "-A")
    run("commit", "-qm", "change")

    t = Task.new("Reword one line in docs/x.md", repo_path=str(repo))
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    orch.reviewer = _Reviewer()

    await orch._run_reviewer(t, repo_path=repo)

    assert not is_trivial(t), "a deleted source file left the bound in place"
    assert AdversarialReviewer._tier_review_turns(t) == _REVIEW_TURNS
    assert any(e.get("kind") == "fast_path" and "revoked" in e["text"]
               for e in events)


async def test_a_planner_CRASH_is_not_called_benign(store, tmp_path,
                                                    monkeypatch):
    """F4: the fast path suppresses R3's RETRY for a crash too (the cap is
    still the cap), but it must not narrate a crash as "a benign skip". Only a
    starved or empty planner is benign; a transport/API failure is told to the
    coder exactly as R3 intended."""
    from no_human.agent.claude_backend import AgentResult

    budgets = []

    class _Planner:
        def __init__(self, *a, **k):
            pass

        async def run(self, prompt, *, cwd=None, max_turns=None, **k):
            budgets.append(max_turns)
            return AgentResult(final_text="API error: connection reset",
                               num_turns=1, is_error=True, tokens_used=1,
                               session_id="s", stop_reason="error")

    import no_human.core.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "ClaudeBackend", _Planner)

    t = Task.new("Drop a phrase from notes/positioning.md",
                 repo_path=str(tmp_path))
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)

    class _Repo:
        path = tmp_path

    assert await orch._generate_plan(t, _Repo()) == ""
    assert budgets == [2], "the tier's cap still holds on a crash"
    texts = [e.get("text", "") for e in events if e.get("kind") == "planning"]
    assert not any("benign" in x for x in texts), texts
    assert t.context.get("plan_unavailable"), "a crash is told to the coder"


def test_changed_paths_reports_a_rename_SOURCE_when_asked_what_moved(tmp_path):
    """Round-3. `git diff --name-status -M` renders a rename as one R entry,
    `R100<TAB>old<TAB>new`, and the parser kept only `parts[-1]` — the
    DESTINATION. So `include_deleted=True` closed the D hole and left its twin
    wide open: a rename REMOVES the source path just as a delete does."""
    from no_human.review.reviewer import _changed_paths

    repo, run = _seeded_repo(tmp_path)
    run("mv", "src/no_human/core/never_push.py", "docs/never_push.md")
    run("commit", "-qm", "rename")

    shown = _changed_paths(repo, "HEAD~1", "HEAD")
    touched = _changed_paths(repo, "HEAD~1", "HEAD", include_deleted=True)
    # The reviewer's own contract is unchanged: show the file that EXISTS.
    assert shown == ["docs/never_push.md"]
    # The checkpoint's contract: everything the diff touched, source included.
    assert set(touched) == {"src/no_human/core/never_push.py",
                            "docs/never_push.md"}


async def test_a_rename_that_deletes_a_source_file_revokes_the_tier(
        store, tmp_path):
    """The exploit the destination-only list allowed: `git mv` a module out of
    `src/` and into `docs/` as prose. Both ends are then ≤2 "prose" files, the
    predicate holds, and a diff that REMOVED the never-push enforcement module
    keeps its 6-turn review."""
    repo, run = _seeded_repo(tmp_path)
    run("mv", "src/no_human/core/never_push.py", "docs/never_push.md")
    run("commit", "-qm", "rename")

    t = Task.new("Move a note into docs/never_push.md", repo_path=str(repo))
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    orch.reviewer = _Reviewer()

    await orch._run_reviewer(t, repo_path=repo)

    assert not is_trivial(t), "a renamed-away source file left the bound in place"
    assert AdversarialReviewer._tier_review_turns(t) == _REVIEW_TURNS


async def test_a_rename_that_deletes_the_instruction_file_revokes_the_tier(
        store, tmp_path):
    """The same move against CLAUDE.md, where BOTH ends are genuinely prose so
    `trivial_paths` holds and only `review_must_be_unbounded` can catch it —
    and it never saw the source path, so it returned False and the file
    carrying "the agent never merges" was deleted under a bounded review."""
    repo, run = _seeded_repo(tmp_path)
    run("mv", "CLAUDE.md", "docs/archive-notes.md")
    run("commit", "-qm", "rename")

    t = Task.new("File the old notes under docs/archive-notes.md",
                 repo_path=str(repo))
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    orch.reviewer = _Reviewer()

    await orch._run_reviewer(t, repo_path=repo)

    assert not is_trivial(t), "the instruction file was renamed away unseen"
    assert AdversarialReviewer._tier_review_turns(t) == _REVIEW_TURNS


def test_instruction_names_match_regardless_of_case():
    """Observation (a): `_repo_instruction_section` resolves these with
    `Path.is_file()`, which is case-INSENSITIVE on APFS — so a repo committing
    `Claude.md` has it injected into every coder session while a case-sensitive
    predicate here would hand its diff the bounded review."""
    assert review_must_be_unbounded(["Claude.md"]) is True
    assert review_must_be_unbounded(["docs/claude.md"]) is True
    assert review_must_be_unbounded(["release_manifest.txt"]) is True


async def test_a_dependency_manifest_diff_gets_the_FULL_review(store, tmp_path,
                                                               monkeypatch):
    """Round-4 finding: requirements.txt / constraints.txt end .txt and read
    as prose, but a machine installs what they name — a one-line dependency
    add runs third-party code, and the coder prompt itself directs dependency
    edits there. Every sibling manifest (pyproject.toml, package.json,
    uv.lock) already escalates by suffix; these two must escalate by name."""
    class _Reviewer:
        async def review(self, task, **kwargs):
            return "decision"

    t = Task.new("pin requests in requirements.txt", repo_path="/r")
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    orch.reviewer = _Reviewer()
    monkeypatch.setattr("no_human.review.reviewer._changed_paths",
                        lambda *a, **k: ["requirements.txt", "docs/x.md"])
    _git_init(tmp_path)

    await orch._run_reviewer(t, repo_path=tmp_path)
    assert not is_trivial(t), (
        "a dependency-manifest diff kept the 6-turn bound")
    assert AdversarialReviewer._tier_review_turns(t) == _REVIEW_TURNS
    assert review_must_be_unbounded(["constraints.txt"]) is True
    assert review_must_be_unbounded(["docs/notes.txt"]) is False
