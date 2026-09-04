"""Review depth scales with diff size — red-first tests.

Covers:
  - the threshold + risk-flag predicate (`core/review_routing.py`), pure and
    fail-closed on every negative branch
  - CONTENT-based guard/scrub detection — the gap a prior review of this
    feature found: `install_pre_push_guard` (`vcs/push_hook.py`), `_redact`
    (`agent/verification_receipts.py`) and `_sanitize_commit_message`
    (`vcs/git.py`) all live in files whose PATHS carry no guard token at
    all, so a path-only check never saw them
  - the orchestrator wiring (`Orchestrator._run_reviewer` routes BEFORE
    `reviewer.review` is ever reached), including the control that proves
    the cheap lane is not a bypass: a small diff that IS risky still gets
    the full review
  - the reviewer's single-turn lane itself (max_turns=1, no tools, fail-closed
    on no-verdict, and a completeness guard that refuses the cheap lane on an
    incomplete prompt regardless of what the router decided)
  - the cost claim (AC#3): the routed lane's turn budget / output tokens are
    a large multiple cheaper than the full multi-round review's, on an
    identical fake backend
"""

from __future__ import annotations

import json
import subprocess

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.config import DEFAULT_CONFIG, load_config
from no_human.core.complexity import is_trivial
from no_human.core.orchestrator import Orchestrator
from no_human.core.review_routing import (
    Entry,
    MAX_SINGLE_TURN_LINES,
    Route,
    changed_entries,
    diff_line_count,
    risk_reason,
    route,
    routing_config,
)
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import (
    AdversarialReviewer,
    ReviewerUnavailable,
    _REVIEW_TURNS,
)


def _block(passed: bool, items: list[dict]) -> str:
    data = {"passed": passed, "items": items}
    return f"REVIEW_JSON_START\n{json.dumps(data)}\nREVIEW_JSON_END\n"


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo


def _repo_with_change(tmp_path, name, mutate):
    """A real git repo, one base commit, then a change commit `mutate` makes."""
    repo = _init_repo(tmp_path, name)
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    mutate(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo


# --------------------------------------------------------------------- #
# The predicate itself — pure, no git                                    #
# --------------------------------------------------------------------- #

def test_threshold_default_is_200_and_is_the_only_source():
    assert MAX_SINGLE_TURN_LINES == 200
    assert DEFAULT_CONFIG["pipeline"]["review_routing"]["max_diff_lines"] == 200
    assert DEFAULT_CONFIG["pipeline"]["review_routing"]["enabled"] is True


@pytest.mark.parametrize("lines, expected", [(200, Route.SINGLE_TURN), (201, Route.FULL_REVIEW)])
def test_a_diff_at_the_threshold_boundary(lines, expected):
    entries = [Entry("M", "src/no_human/docs_gen.py")]
    got, why = route(entries=entries, line_count=lines, enabled=True, max_lines=200)
    assert got is expected, why


def test_disabled_config_routes_full_regardless_of_size():
    entries = [Entry("M", "README.md")]
    got, why = route(entries=entries, line_count=1, enabled=False, max_lines=200)
    assert got is Route.FULL_REVIEW
    assert "disabled" in why


def test_empty_diff_routes_full():
    got, why = route(entries=[], line_count=0, enabled=True, max_lines=200)
    assert got is Route.FULL_REVIEW
    assert "no readable changed paths" in why


def test_unreadable_line_count_routes_full():
    entries = [Entry("M", "README.md")]
    got, why = route(entries=entries, line_count=None, enabled=True, max_lines=200)
    assert got is Route.FULL_REVIEW
    assert "unreadable" in why


@pytest.mark.parametrize("raw, expect_enabled, expect_max", [
    ({"enabled": True, "max_diff_lines": 200}, True, 200),
    ({"enabled": False, "max_diff_lines": 200}, False, 200),
    ({"enabled": True, "max_diff_lines": 0}, False, 200),       # non-positive fails closed
    ({"enabled": True, "max_diff_lines": -5}, False, 200),
    ({"enabled": True, "max_diff_lines": "oops"}, False, 200),  # non-int fails closed
    ({"enabled": True}, True, 200),                             # default max_diff_lines
])
def test_routing_config_fails_closed_on_bad_values(raw, expect_enabled, expect_max):
    enabled, max_lines = routing_config({"pipeline": {"review_routing": raw}})
    assert enabled is expect_enabled
    assert max_lines == expect_max


def test_routing_config_tolerates_the_pipeline_none_deep_merge_shape():
    """Mirrors `complexity.trivial_enabled`'s contract: `pipeline:` with its
    body commented out deep-merges to `None`, not an absent key."""
    enabled, max_lines = routing_config({"pipeline": None})
    assert enabled is True
    assert max_lines == 200
    assert routing_config(None) == (True, 200)
    assert routing_config({}) == (True, 200)


# --------------------------------------------------------------------- #
# Risk flags — path-based (each a synthetic small diff)                  #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("status, path, family", [
    ("D", "tests/test_x.py", "test deletion"),
    ("D", "test/x_test.py", "test deletion (alt root)"),
    ("D", "e2e/conftest.py", "conftest deletion"),
    ("M", "src/no_human/vcs/never_push.py", "guard-token path"),
    ("M", "src/no_human/vcs/outbound_scrub.py", "scrub-token path"),
    ("M", "src/no_human/review/some_helper.py", "review/** root"),
    ("M", "src/no_human/ci_gate/gate.py", "ci_gate/** root"),
    ("M", "src/no_human/config.py", "config.py exact path"),
    ("M", ".github/workflows/ci.yml", "workflow"),
    ("M", ".githooks/pre-push", "githook"),
    ("M", "src/no_human/brain/keys.py", "key-token path"),
    ("M", "config.yaml", "config.yaml basename"),
    ("M", "src/no_human/agent/secrets_store.py", "secret-token path"),
    ("M", "EXPORT_CLASSIFICATION.txt", "gate control data"),
    ("M", ".agents/reviewer.md", "agent instructions"),
])
def test_risk_flags_route_full_at_any_size(status, path, family):
    entries = [Entry(status, path)]
    reason = risk_reason(entries)
    assert reason is not None, family
    got, why = route(entries=entries, line_count=3, enabled=True, max_lines=200)
    assert got is Route.FULL_REVIEW, family


def test_a_test_file_renamed_away_is_flagged_by_its_source_side():
    """`R100<TAB>old<TAB>new` from git yields TWO entries (one per side, see
    `changed_entries`); the OLD side is what makes this a test deletion in
    substance even though the file still exists under a new name."""
    entries = [Entry("R100", "tests/test_old.py"), Entry("R100", "src/renamed_out.py")]
    reason = risk_reason(entries)
    assert reason is not None and "renamed" in reason


def test_a_benign_small_diff_has_no_risk_reason():
    entries = [Entry("M", "src/no_human/docs_gen.py"), Entry("A", "docs/notes.md")]
    assert risk_reason(entries) is None
    got, why = route(entries=entries, line_count=20, enabled=True, max_lines=200)
    assert got is Route.SINGLE_TURN, why


# --------------------------------------------------------------------- #
# Risk flags — CONTENT-based guard/scrub detection (real git)            #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("func_name, token", [
    ("install_pre_push_guard", "guard"),      # src/no_human/vcs/push_hook.py
    ("_redact", "redact"),                    # src/no_human/agent/verification_receipts.py
    ("_sanitize_commit_message", "sanitiz"),  # src/no_human/vcs/git.py
])
def test_content_based_guard_detection_catches_real_function_names(tmp_path, func_name, token):
    """None of these three functions' FILE paths carry a guard token — only
    scanning the diff's hunk content, not just the path, catches them."""
    repo = _init_repo(tmp_path, name=f"repo-{func_name}")
    (repo / "generic_module.py").write_text("def existing():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "generic_module.py").write_text(
        f"def existing():\n    return 1\n\n\ndef {func_name}(x):\n    return x\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add function")

    entries = changed_entries(repo, "HEAD~1", "HEAD")
    assert not any(token in e.path.casefold() for e in entries), (
        "the path itself must carry no guard token — that is the gap being tested"
    )
    # Path-only check finds nothing:
    assert risk_reason(entries) is None
    # Content-aware check does:
    reason = risk_reason(entries, repo, "HEAD~1", "HEAD")
    assert reason is not None and token in reason

    lines = diff_line_count(repo, "HEAD~1", "HEAD")
    got, why = route(
        entries=entries, line_count=lines, enabled=True, max_lines=200,
        repo_path=repo, before="HEAD~1", after="HEAD",
    )
    assert got is Route.FULL_REVIEW, why


def test_content_scan_ignores_diff_file_headers():
    """`+++`/`---` file header lines are not hunk content — a path containing
    a guard token there must not double-count against the content scan (the
    path check already covers it), and a header alone must not false-positive
    when the path itself is benign."""
    from no_human.core.review_routing import _content_guard_reason

    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old = 1\n"
        "+new = 1\n"
    )
    assert _content_guard_reason(diff) is None


# --------------------------------------------------------------------- #
# Fail-closed on unreadable git state                                    #
# --------------------------------------------------------------------- #

def test_diff_line_count_none_on_binary(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "a.bin").write_bytes(b"\x00\x01\x02")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "a.bin").write_bytes(b"\x00\x01\x02\x03")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    assert diff_line_count(repo, "HEAD~1", "HEAD") is None
    got, why = route(
        entries=changed_entries(repo, "HEAD~1", "HEAD"),
        line_count=diff_line_count(repo, "HEAD~1", "HEAD"),
        enabled=True, max_lines=200,
    )
    assert got is Route.FULL_REVIEW


def test_changed_entries_raises_on_a_non_repo(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with pytest.raises(Exception):
        changed_entries(not_a_repo, "HEAD~1", "HEAD")


# --------------------------------------------------------------------- #
# Orchestrator wiring — `_run_reviewer` routes before `reviewer.review`  #
# --------------------------------------------------------------------- #

class _Backend:
    async def run(self, *a, **k):  # pragma: no cover
        raise AssertionError("backend should not run here")


class _RecordingReviewer:
    def __init__(self):
        self.kwargs = None

    async def review(self, task, **kwargs):
        self.kwargs = kwargs
        return "decision"


def _orch(store, tmp_path, events, cfg_overlay=None):
    cfg = load_config(tmp_path / "config.yaml")
    if cfg_overlay:
        cfg.data.update(cfg_overlay)
    return Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)


def _task(**kw):
    defaults = dict(id="aaa", source="test", title="t",
                    status=TaskStatus.PENDING, acceptance_criteria=[])
    defaults.update(kw)
    return Task(**defaults)


async def test_a_small_benign_diff_takes_the_single_turn_gate(store, tmp_path):
    def mutate(repo):
        (repo / "src" / "main.py").write_text("x = 1\ny = 2\n")

    repo = _repo_with_change(tmp_path, "benign", mutate)
    t = _task(repo_path=str(repo))
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    reviewer = _RecordingReviewer()
    orch.reviewer = reviewer

    await orch._run_reviewer(t, repo_path=repo)

    assert reviewer.kwargs is not None
    assert reviewer.kwargs.get("single_turn") is True
    routed = [e for e in events if e.get("kind") == "review_routing"]
    assert routed and routed[0]["route"] == "single_turn"


async def test_a_small_risky_diff_takes_the_full_review(store, tmp_path):
    """Control for the test above: SAME size, same repo, same harness — one
    extra file swapped for a guard-sounding path. Proves the cheap lane is
    not a bypass for what the reviewer exists to catch."""
    def mutate(repo):
        (repo / "src" / "main.py").write_text("x = 1\ny = 2\n")
        (repo / "src" / "never_push.py").write_text("GUARD = True\n")

    repo = _repo_with_change(tmp_path, "risky", mutate)
    t = _task(repo_path=str(repo))
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    reviewer = _RecordingReviewer()
    orch.reviewer = reviewer

    await orch._run_reviewer(t, repo_path=repo)

    assert reviewer.kwargs is not None
    assert not reviewer.kwargs.get("single_turn")
    routed = [e for e in events if e.get("kind") == "review_routing"]
    assert routed and routed[0]["route"] == "full_review"
    assert "never_push" in routed[0]["text"] or "guard" in routed[0]["text"]


async def test_routing_never_overrides_the_trivial_tier_escalation(store, tmp_path):
    def mutate(repo):
        (repo / ".agents").mkdir(exist_ok=True)
        (repo / ".agents" / "reviewer.md").write_text("rule\n")

    repo = _repo_with_change(tmp_path, "trivial-agents", mutate)
    t = _task(repo_path=str(repo))
    t.context = {"complexity_tier": "trivial"}
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    reviewer = _RecordingReviewer()
    orch.reviewer = reviewer

    await orch._run_reviewer(t, repo_path=repo)

    assert not is_trivial(t), "the trivial-tier checkpoint must revoke first"
    assert reviewer.kwargs is not None
    assert not reviewer.kwargs.get("single_turn")


async def test_a_second_review_round_gets_the_full_review(store, tmp_path):
    def mutate(repo):
        (repo / "src" / "main.py").write_text("x = 1\ny = 2\n")

    repo = _repo_with_change(tmp_path, "round2", mutate)
    t = _task(repo_path=str(repo))
    await store.create_task(t)
    events = []
    orch = _orch(store, tmp_path, events)
    reviewer = _RecordingReviewer()
    orch.reviewer = reviewer

    await orch._run_reviewer(t, repo_path=repo,
                             prior_rounds="  - round 1 [FAIL]: something")

    assert reviewer.kwargs is not None
    assert not reviewer.kwargs.get("single_turn")
    assert not any(e.get("kind") == "review_routing" for e in events)


async def test_disabled_config_restores_todays_behaviour(store, tmp_path):
    def mutate(repo):
        (repo / "src" / "main.py").write_text("x = 1\ny = 2\n")

    repo = _repo_with_change(tmp_path, "disabled", mutate)
    t = _task(repo_path=str(repo))
    await store.create_task(t)
    events = []
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data["pipeline"]["review_routing"] = {"enabled": False}
    orch = Orchestrator(store, cfg.data, _Backend(), SlackNotifier(None),
                        event_sink=events.append)
    reviewer = _RecordingReviewer()
    orch.reviewer = reviewer

    await orch._run_reviewer(t, repo_path=repo)

    assert reviewer.kwargs is not None
    assert not reviewer.kwargs.get("single_turn")
    routed = [e for e in events if e.get("kind") == "review_routing"]
    assert routed and "disabled" in routed[0]["text"]


async def test_caller_supplied_single_turn_kwarg_raises(store, tmp_path):
    orch = _orch(store, tmp_path, [])
    t = _task(repo_path=str(tmp_path))
    with pytest.raises(TypeError):
        await orch._run_reviewer(t, repo_path=tmp_path, single_turn=True)


# --------------------------------------------------------------------- #
# The reviewer's single-turn lane itself                                 #
# --------------------------------------------------------------------- #

def _small_change_repo(tmp_path, name="calc-repo"):
    repo = _init_repo(tmp_path, name)
    (repo / "calc.py").write_text("def add(a, b): return a + b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / "calc.py").write_text(
        "def add(a, b): return a + b\n\ndef sub(a, b): return a - b\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add sub")
    return repo


async def test_single_turn_lane_runs_max_turns_1_with_no_tools(tmp_path):
    repo = _small_change_repo(tmp_path)
    calls = []

    class CapturingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      resume=None, on_event=None, supervisor_hook=None):
            calls.append({"prompt": prompt, "max_turns": max_turns})
            out = _block(True, [{"label": "ok", "passed": True,
                                 "evidence": "calc.py:3", "severity": "low"}])
            return AgentResult(final_text=out, num_turns=1, is_error=False,
                               tokens_used=100, session_id="f",
                               stop_reason="end_turn")

    reviewer = AdversarialReviewer(backend=CapturingBackend())
    t = Task.new("add sub()")
    t.acceptance_criteria = ["sub(a,b) returns difference"]
    decision = await reviewer.review(t, repo_path=repo, single_turn=True)

    assert decision.passed is True
    assert len(calls) == 1
    assert calls[0]["max_turns"] == 1
    assert "CRITICAL: Do NOT use any tools" in calls[0]["prompt"]


async def test_completeness_guard_falls_back_to_multi_turn_on_omitted_files(
        tmp_path, monkeypatch):
    """A small-by-line-count diff to a file so large `_full_file_context`
    omits it must NOT run single-turn: `allow_tools=False` would forbid
    reading exactly the file the router assumed was already in the prompt."""
    repo = _small_change_repo(tmp_path, "omitted-repo")

    import no_human.review.reviewer as reviewer_mod
    monkeypatch.setattr(reviewer_mod, "_full_file_context",
                        lambda *a, **k: ("", ["huge_untouched.py"]))

    calls = []

    class CapturingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      resume=None, on_event=None, supervisor_hook=None):
            calls.append({"max_turns": max_turns})
            out = _block(True, [{"label": "ok", "passed": True,
                                 "evidence": "calc.py:3", "severity": "low"}])
            return AgentResult(final_text=out, num_turns=3, is_error=False,
                               tokens_used=100, session_id="f",
                               stop_reason="end_turn")

    reviewer = AdversarialReviewer(backend=CapturingBackend())
    t = Task.new("add sub()")
    decision = await reviewer.review(t, repo_path=repo, single_turn=True)

    assert decision.passed is True
    assert calls[0]["max_turns"] > 1, "must have fallen back to the multi-turn path"


async def test_no_verdict_on_the_single_turn_gate_escalates(tmp_path):
    repo = _small_change_repo(tmp_path, "flaky-repo")

    class FlakyBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      resume=None, on_event=None, supervisor_hook=None):
            return AgentResult(
                final_text="I could not determine if this is done.",
                num_turns=1, is_error=False, tokens_used=50,
                session_id="f", stop_reason="end_turn",
            )

    reviewer = AdversarialReviewer(backend=FlakyBackend())
    t = Task.new("add sub()")
    with pytest.raises(ReviewerUnavailable):
        await reviewer.review(t, repo_path=repo, single_turn=True)


# --------------------------------------------------------------------- #
# Cost claim (AC#3)                                                      #
# --------------------------------------------------------------------- #

async def test_single_turn_gate_costs_less_than_half_the_full_review(tmp_path):
    """Instrument A (deterministic, in CI): a fake backend whose output
    tokens are proportional to the turn budget it was granted — the turn
    budget IS the cost driver the exploration-turn reduction targets (the
    reviewer model/prompt/checklist are unchanged, see reviewer.py:109's
    'WHAT THIS KNOB DOES NOT DECIDE' note). single-turn (max_turns=1) vs
    full (max_turns=_REVIEW_TURNS=30): a 1/30 ratio, far past the 50% bar.

    Instrument B (real spend) is reported, not asserted, in the PR body:
    no real gate-review rounds exist on this fresh branch to average, so the
    PR states that plainly rather than inventing numbers.
    """
    repo = _small_change_repo(tmp_path, "cost-repo")

    class MeteredBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      resume=None, on_event=None, supervisor_hook=None):
            out = _block(True, [{"label": "ok", "passed": True,
                                 "evidence": "calc.py:3", "severity": "low"}])
            return AgentResult(
                final_text=out, num_turns=max_turns, is_error=False,
                tokens_used=max_turns * 1000, session_id="f",
                stop_reason="end_turn", output_tokens=max_turns * 1000,
            )

    reviewer = AdversarialReviewer(backend=MeteredBackend())
    t = Task.new("add sub()")

    single = await reviewer.review(t, repo_path=repo, single_turn=True)
    full = await reviewer.review(t, repo_path=repo, single_turn=False)

    assert single.output_tokens == 1000
    assert full.output_tokens == _REVIEW_TURNS * 1000
    assert single.output_tokens <= 0.5 * full.output_tokens
