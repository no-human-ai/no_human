"""The review gate fails closed when no reviewer is wired (M0.3).

The reviewer is the only gate between an unreviewed diff and a PR. Returning a
passing decision when it is absent turns the hard gate into a silent rubber
stamp. `nh watch` did exactly that in production.
"""

import ast
import pathlib
from types import SimpleNamespace

import pytest

from no_human.config import DEFAULT_CONFIG, load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.review.reviewer import ReviewerUnavailable

from .test_e2e_orchestrator import FakeBackend, _config, bare_repo  # noqa: F401


def _good_mutate(cwd):
    """A real, passing diff — so the task reaches the review gate rather than
    tripping the zero-diff breaker first."""
    (cwd / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    (cwd / "test_calc.py").write_text(
        "from calc import add, mul\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_mul():\n    assert mul(2, 3) == 6\n"
    )


def test_default_config_fails_closed():
    assert DEFAULT_CONFIG["reviewer"]["allow_advisory"] is False


async def test_run_review_raises_when_no_reviewer_is_wired(store, tmp_path):
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    orch = Orchestrator(store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None))
    with pytest.raises(ReviewerUnavailable, match="rubber stamp"):
        await orch._run_review(Task.new("t", repo_path="/r"), None, "attempt-1")


async def test_advisory_pass_through_requires_the_explicit_flag(store, tmp_path):
    """Opting in is allowed for eval/replay — but it is announced, never silent."""
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = True
    events = []
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        event_sink=events.append,
    )
    decision = await orch._run_review(Task.new("t", repo_path="/r"), None, "attempt-1")

    assert decision.passed is True
    (advisory,) = [e for e in events if e["kind"] == "review_advisory"]
    assert "NOT reviewed" in advisory["text"]


@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_a_missing_reviewer_escalates_instead_of_opening_a_pr(
    store, bare_repo, tmp_path
):
    """End to end: reverting the fail-closed guard lets this task reach
    AWAITING_APPROVAL with an unreviewed diff."""
    cfg = _config(tmp_path)
    cfg.data["reviewer"]["allow_advisory"] = False
    orch = Orchestrator(store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None))

    task = Task.new("add add()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(task)
    outcome = await orch.run_task(task)

    assert outcome.status is not TaskStatus.AWAITING_APPROVAL
    assert outcome.pr_url is None
    assert "no reviewer is configured" in outcome.detail


def test_no_production_orchestrator_is_built_without_a_reviewer():
    """`nh watch` built its own Orchestrator and forgot the reviewer, so it drove
    tasks to a PR with the gate pass-through. Nothing caught it: the constructor
    defaults `reviewer=None`. This is the guard that would have."""
    missing = []
    for path in pathlib.Path("src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "Orchestrator"
                and "reviewer" not in {kw.arg for kw in node.keywords}
            ):
                missing.append(f"{path}:{node.lineno}")
    assert not missing, (
        "production code must always wire the review gate; missing at: "
        + ", ".join(missing)
    )


# ------- the reviewer that reached no verdict (task 84251cb2, attempt 13) ---- #


class _FakeAgentResult:
    def __init__(self, final_text, *, is_error=False, stop_reason="end_turn",
                 tokens_used=0, cache_read_tokens=0, cache_creation_tokens=0,
                 output_tokens=None):
        self.final_text = final_text
        self.is_error = is_error
        self.stop_reason = stop_reason
        self.num_turns = 11
        self.tokens_used = tokens_used
        self.cache_read_tokens = cache_read_tokens
        self.cache_creation_tokens = cache_creation_tokens
        self.output_tokens = output_tokens
        self.session_id = "s"


class _ReviewerBackend:
    """Replays a scripted sequence of reviewer sessions."""

    def __init__(self, *results):
        self._results = list(results)
        self.budgets: list[int] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kw):
        self.budgets.append(max_turns)
        return self._results.pop(0)


# NB: the parser reads "items", not "checklist". This fixture originally used
# "checklist", so items parsed as [] — and the test only passed because of the
# vacuous-pass bug (empty checklist + passed:true ⇒ pass) the gate fix removed.
_VERDICT = (
    'REVIEW_JSON_START {"passed": true, "items": '
    '[{"label": "ok", "passed": true, "evidence": "calc.py:1"}]} REVIEW_JSON_END'
)
_MAX_TURNS_ERROR = "Claude Code returned an error result: Reached maximum number of turns (10)"


async def test_reviewer_out_of_turns_escalates_instead_of_blaming_the_coder(tmp_path):
    """Attempt 13 of task 84251cb2: the reviewer exhausted its own turn budget
    while reading files and never emitted REVIEW_JSON. The fail-closed decision
    was fed back as a coder finding ("reviewer produced no parseable
    REVIEW_JSON") and spent the task's last bounded attempt.

    Reverting `_agent_review`'s retry+raise makes this return passed=False,
    i.e. a finding the coder is then told to fix."""
    from no_human.review.reviewer import AdversarialReviewer

    backend = _ReviewerBackend(
        _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True, stop_reason="max_turns"),
        _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True, stop_reason="max_turns"),
    )
    reviewer = AdversarialReviewer(backend=backend)

    with pytest.raises(ReviewerUnavailable, match="no verdict"):
        await reviewer._agent_review("prompt", tmp_path)

    assert len(backend.budgets) == 2, "one bounded infra retry (constraint #4)"
    assert backend.budgets[1] > backend.budgets[0], "the retry gets more turns"


async def test_a_reviewer_that_recovers_on_the_retry_returns_its_verdict(tmp_path):
    from no_human.review.reviewer import AdversarialReviewer

    backend = _ReviewerBackend(
        _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True, stop_reason="max_turns"),
        _FakeAgentResult(_VERDICT),
    )
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert decision.passed is True


async def test_review_timeout_halves_the_retry_window_on_a_hang(tmp_path, monkeypatch):
    """A hung/saturated reviewer (timeout, not turn exhaustion) must not be
    granted a second FULL window — a 50-line diff sat 20min in review (2×600s)
    in prod. The retry's window is halved; the task escalates ~5min sooner."""
    import asyncio
    import time as _time

    import no_human.review.reviewer as rv

    # Small floor so the shrink is observable in a fast test.
    monkeypatch.setattr(rv, "_REVIEW_MIN_RETRY_TIMEOUT", 0.05)

    windows: list[float] = []

    class _HangingBackend:
        async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kw):
            start = _time.monotonic()
            try:
                await asyncio.sleep(5)  # never finishes inside the wait_for window
            except asyncio.CancelledError:
                windows.append(_time.monotonic() - start)
                raise
            return _FakeAgentResult(_VERDICT)

    with pytest.raises(ReviewerUnavailable, match="no verdict"):
        await rv.AdversarialReviewer(backend=_HangingBackend())._agent_review(
            "p", tmp_path, timeout=0.4
        )

    assert len(windows) == 2, "one bounded infra retry (constraint #4)"
    # Round 2's window was ~half of round 1's — not another full 0.4s.
    assert windows[1] < windows[0] * 0.75


async def test_a_real_failing_verdict_is_never_retried_or_swallowed(tmp_path):
    """A genuine FAIL is a finding, not an infra error: return it on round one."""
    from no_human.review.reviewer import AdversarialReviewer

    # "items", not "checklist" — with the wrong key this asserted False for the
    # wrong reason (empty checklist fails closed) instead of the failing finding.
    fail = (
        'REVIEW_JSON_START {"passed": false, "items": '
        '[{"label": "bug", "passed": false, "severity": "high", '
        '"evidence": "calc.py:3"}]} REVIEW_JSON_END'
    )
    backend = _ReviewerBackend(_FakeAgentResult(fail))
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert decision.passed is False
    assert len(backend.budgets) == 1, "a real finding must not trigger an infra retry"


async def test_reviewer_turn_budget_outgrew_the_pre_D16_default():
    """The 10-turn cap predates the reviewer being able to read files."""
    from no_human.review import reviewer as r
    assert r._REVIEW_TURNS >= 30


async def test_run_review_does_not_convert_no_verdict_into_a_coder_finding(
    store, tmp_path, bare_repo
):
    """`_run_review`'s `except Exception` used to swallow ReviewerUnavailable and
    return a failing ReviewDecision — re-poisoning the coder's feedback."""
    from no_human.core.orchestrator import Orchestrator

    class _DeadReviewer:
        model = "claude-opus-5"
        _on_event = None
        async def review(self, *a, **kw):
            raise ReviewerUnavailable("reviewer reached no verdict")

    cfg = _config(tmp_path)
    orch = Orchestrator(
        store, cfg.data, FakeBackend(_good_mutate), SlackNotifier(None),
        reviewer=_DeadReviewer(),
    )
    task = Task.new("t", repo_path=str(bare_repo))
    await store.create_task(task)
    with pytest.raises(ReviewerUnavailable):
        await orch._run_review(task, orch._open_repo(task), "attempt-1")


def test_the_gate_prompt_tells_the_reviewer_the_pr_exists_0a():
    """0a / PR-021 — the gate ran BEFORE the PR existed.

    `_run_review` is called from `_run_attempt`; `open_pr` lived only in `_finalize`,
    which runs later. So a criterion of the form "the PR body contains X" was judged
    when no PR existed. Evidence (task abc7e570): three attempts, 4.89M tokens, ZERO
    PRs, each failing on "Required PR-body evidence still missing". The plan names it
    the root cause of PR-011 (10.33M, no PR) and says it REFRAMES PR-015 — a reliable
    judge applying an impossible rule, not an inconsistent judge.

    🔴 THE ORDERING WAS WRONG, NOT THE CRITERION. A bugfix with no demonstrated RED is
    exactly what this product exists to stop shipping, so the criterion stays and the
    artifact it names is created first.

    This asserts the PROMPT, because the plumbing being present proves nothing — a
    kwarg accepted and never interpolated is the defect that took five review rounds on
    a different branch. Both directions matter: present, and honestly absent.
    """
    from no_human.core.task import Task
    from no_human.review.reviewer import _build_review_prompt

    task = Task.new("add mul()", repo_path="/tmp/r")
    task.acceptance_criteria = ["the PR body contains a demonstrated RED"]

    with_pr = _build_review_prompt(
        task, "diff", "", "", draft_pr="https://github.com/o/r/pull/7")
    assert "pull/7" in with_pr, (
        "the draft PR url never reaches the prompt — the reviewer cannot judge a "
        "PR-body criterion against a PR it was never told about"
    )
    assert "judge it against" in with_pr, (
        "the prompt mentions the PR but does not instruct the reviewer to judge the "
        "criterion against it"
    )
    assert "cannot author" in with_pr, (
        "the prompt must say the body is template-generated; otherwise the reviewer "
        "faults the implementer for headings it could not have written"
    )

    # 🔴 THE ABSENCE TEXT IS CONDITIONAL NOW, and that is the point. My first version
    # emitted "the forge was unreachable when one was attempted" whenever there was no
    # url — false for every GitLab remote, every local bare repo (the entire bench corpus
    # and these fixtures), and _gate_already_satisfied, none of which attempt an open. A
    # false causal claim in the one component whose value is evidence-based judgement,
    # on the majority of runs — and it made the diff non-bench-neutral.
    attempted_and_failed = _build_review_prompt(
        task, "diff", "", "", draft_pr_absent="open failed")
    assert "artifact is absent" in attempted_and_failed, (
        "when an open was attempted and FAILED, the prompt must say the artifact is "
        "genuinely missing"
    )
    assert "do NOT ask the implementer to open a PR" in attempted_and_failed, (
        "the original failure mode was the reviewer instructing the coder to open a PR — "
        "something only the loop can do, which burned three attempts on abc7e570"
    )

    # No attempt made (GitLab, local bare repo, bench): SILENT. Byte-identical to main for
    # those runs, which is the only defensible default for a diff that must be
    # bench-neutral.
    not_attempted = _build_review_prompt(task, "diff", "", "")
    assert "unreachable" not in not_attempted, (
        "the prompt claims the forge was unreachable when no open was ever attempted"
    )
    assert "artifact is absent" not in not_attempted, (
        "the prompt excuses a PR-body criterion on a run where the PR is opened at "
        "delivery and the criterion IS satisfiable — the opposite of honest judgement"
    )


async def _async_noop(*_a, **_kw):
    """Minimal awaitable store double — see the note at each call site."""
    return None


async def test_the_draft_helper_actually_RETURNS_the_url_it_opened_0a(tmp_path,
                                                                      monkeypatch):
    """🔴 MY SECOND ATTEMPT AT THIS TEST WAS A TAUTOLOGY.

    The first was `src.index()` arithmetic, which a review defeated two ways. I replaced
    it with a "runtime" test that called a spy reviewer with a HARDCODED url and asserted
    the kwarg arrived — i.e. it asserted that Python passes arguments. It tested nothing
    about the orchestrator, and the mutation it was written to kill (the helper returning
    "" unconditionally) sailed straight through it.

    This one drives `_open_draft_pr_for_review` itself with `open_pr` stubbed, so the
    assertion is about OUR code: the helper must open a PR on a GitHub remote and return
    that url. If it returns "" the whole fix is inert and the reviewer is told no PR
    exists.
    """
    from types import SimpleNamespace

    import no_human.core.orchestrator as orch_mod
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task

    opened: list[tuple[str, str]] = []

    def fake_open_pr(repo, branch, title, body, **kw):
        opened.append((branch, body))
        return SimpleNamespace(url="https://github.com/o/r/pull/42")

    monkeypatch.setattr(orch_mod, "open_pr", fake_open_pr)

    orch = Orchestrator.__new__(Orchestrator)
    orch._sink = lambda e: None
    orch._active_attempt_id = None
    orch.config = {"git": {"github_hosts": []}}
    # The helper now stakes a DURABLE claim ("this run created the draft, so this run
    # may rewrite its body") in task.context, because an in-process attribute could not
    # survive a park/resume — a review drove that defect. A store is therefore a real
    # collaborator here, not stub scaffolding.
    orch.store = SimpleNamespace(update_task=_async_noop)

    repo = SimpleNamespace(
        remote_url=lambda: "https://github.com/o/r.git", path=tmp_path)
    task = Task.new("add mul()", repo_path=str(tmp_path))
    task.acceptance_criteria = ["the PR body contains a demonstrated RED"]
    result = SimpleNamespace(final_text="did the thing", num_turns=3)
    commit = SimpleNamespace(files_changed=1, insertions=2, deletions=0, sha="abc1234")

    url = await orch._open_draft_pr_for_review(
        task, repo, "nh/task-1", "main", "att-1", commit=commit, result=result)

    assert url == "https://github.com/o/r/pull/42", (
        f"the helper opened a PR but returned {url!r} — the reviewer is then told no PR "
        f"exists and a PR-body criterion fails for a reason the coder cannot act on, "
        f"which is the exact defect 0a fixes"
    )
    assert opened and opened[0][0] == "nh/task-1", "no PR was opened at all"
    # And the body it opened with must be the real one, not a placeholder: the reviewer
    # judges this text.
    assert "add mul()" in opened[0][1] or "demonstrated RED" in opened[0][1], (
        f"the PR body the gate will judge carries neither the task nor its criteria: "
        f"{opened[0][1][:200]!r}"
    )


async def test_a_non_github_remote_gets_no_pre_gate_pr_0a(tmp_path, monkeypatch):
    """CRITICAL-1: on GitLab this must not open anything.

    `gitlab.open_mr` has no already-exists branch and passes no `--draft`, so a pre-gate
    open there made `_finalize`'s open raise twice and ESCALATED a task that had PASSED
    review — driven and confirmed by review (AWAITING_APPROVAL on main -> ESCALATED).
    """
    from types import SimpleNamespace

    import no_human.core.orchestrator as orch_mod
    from no_human.core.orchestrator import Orchestrator
    from no_human.core.task import Task

    calls: list = []
    monkeypatch.setattr(orch_mod, "open_pr",
                        lambda *a, **k: calls.append(1) or SimpleNamespace(url="x"))

    orch = Orchestrator.__new__(Orchestrator)
    orch._sink = lambda e: None
    orch._active_attempt_id = None
    orch.config = {"git": {"github_hosts": []}}
    # The helper now stakes a DURABLE claim ("this run created the draft, so this run
    # may rewrite its body") in task.context, because an in-process attribute could not
    # survive a park/resume — a review drove that defect. A store is therefore a real
    # collaborator here, not stub scaffolding.
    orch.store = SimpleNamespace(update_task=_async_noop)

    repo = SimpleNamespace(
        remote_url=lambda: "https://gitlab.com/o/r.git", path=tmp_path)
    task = Task.new("x", repo_path=str(tmp_path))

    url = await orch._open_draft_pr_for_review(
        task, repo, "nh/task-1", "main", "att-1",
        commit=SimpleNamespace(files_changed=0, insertions=0, deletions=0, sha="a"),
        result=SimpleNamespace(final_text="t", num_turns=1))

    assert url == "" and not calls, (
        "a pre-gate PR was opened against a non-GitHub remote. GitLab is neither "
        "draft-by-default nor idempotent, so _finalize's open then fails twice and a "
        "PASSING task escalates."
    )


def test_the_draft_pr_is_opened_before_the_gate_not_after_0a():
    """Ordering lint. Explicitly a BACKSTOP, not the proof.

    Kept only because it catches a whole-hog reordering cheaply. A review demonstrated it
    cannot see a url dropped at the second seam, nor a helper that returns "" always —
    tests/test_e2e_orchestrator.py::test_the_draft_pr_url_REACHES_the_reviewer_end_to_end_0a
    is what covers those. (This line named a test that never existed for two commits; a
    review caught it twice. Both names in this docstring are real as of this commit.)
    """
    import inspect

    import no_human.core.orchestrator as _mod

    src = inspect.getsource(_mod)
    draft = src.index("_open_draft_pr_for_review(")
    review = src.index("decision = await self._run_review(")
    assert draft < review, (
        "the draft PR is opened AFTER the review gate runs — which is the original "
        "defect: the criterion refers to an artifact that does not exist yet"
    )
    # 🔴 GITHUB-ONLY GUARD. Without it, GitLab escalates a task that PASSED review:
    # gitlab.open_mr has no already-exists branch, so _finalize's open raises twice.
    assert "is_github_remote" in src, (
        "the pre-gate draft open is not gated to GitHub. gitlab.open_mr is neither "
        "draft-by-default nor idempotent, and a duplicate MR turns a passing task into "
        "an escalation — driven and confirmed by review."
    )


# The `_run_review` -> `reviewer.review` seam is covered, but NOT here — it needs the
# driven harness, so it lives in
#   tests/test_e2e_orchestrator.py::test_the_draft_pr_url_REACHES_the_reviewer_end_to_end_0a
# It drives the real run_task with a stubbed FORGE (orch_mod.open_pr) and a spy reviewer,
# and asserts both 0a properties: open_pr happens BEFORE review, and the url the forge
# returned is the url the gate received. Verified to kill two mutants that this file's
# tests do not see: dropping `draft_pr=` at the seam, and removing the helper call
# entirely. Two earlier attempts at the same seam were worthless and are recorded so
# they are not repeated — one passed a hardcoded url to a spy and asserted that Python
# passes arguments; the other stubbed Orchestrator and grew an attribute per run
# (_usable_profile, _test_cache, ...) until it was a mock testing the mock.


def test_the_already_exists_path_refreshes_the_body_only_when_asked_0a(tmp_path,
                                                                      monkeypatch):
    """The C-2 fix had ZERO coverage — deleting it passed the whole suite.

    And unconditional editing would have OVERWRITTEN a PR description a human edited, on
    the revision flow (a task resuming onto an existing PR branch). So the update is
    opt-in and only `_finalize` asks for it, after this run opened the draft itself.
    """
    import subprocess as _sp

    from no_human.vcs import github as gh

    argv: list[list[str]] = []

    class Done:
        returncode = 1
        stdout = ""
        stderr = "a pull request for branch already exists"

    class Ok:
        returncode = 0
        stdout = "https://github.com/o/r/pull/9"
        stderr = ""

    def fake_run(cmd, **kw):
        argv.append(cmd)
        if cmd[:3] == ["gh", "pr", "create"]:
            return Done()
        if cmd[:3] == ["gh", "pr", "list"]:
            return Ok()
        if cmd[:3] == ["gh", "pr", "edit"]:
            return Ok()
        return Ok()

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr(gh.subprocess, "run", fake_run)

    # Default: NO body rewrite — a human's edits survive.
    argv.clear()
    gh.open_pr(tmp_path, "br", "t", "new body")
    assert not any(c[:3] == ["gh", "pr", "edit"] for c in argv), (
        "the body was rewritten without being asked — this path is also the revision "
        "flow, where it would discard a description a human edited"
    )

    # Opt-in: the run that opened the draft refreshes it with the evidence-bearing body.
    argv.clear()
    gh.open_pr(tmp_path, "br", "t", "new body", update_existing_body=True)
    edits = [c for c in argv if c[:3] == ["gh", "pr", "edit"]]
    assert edits, "update_existing_body=True did not refresh the body"
    assert edits[0][-1] == "new body" and "--body" in edits[0], (
        f"the edit did not carry the new body: {edits[0]!r}"
    )


# ---- R5: a TRUNCATED reviewer's verdict is not a verdict ------------------- #
#
# 4 August tasks hit a reviewer session that ran out of turns and still carried
# a parseable REVIEW_JSON block. `_review_once` gated on `result.is_error` only
# inside the branch where parsing had ALSO found nothing, so the mid-flight
# verdict was accepted as the reviewer's conclusion. Two of the four did
# demonstrable damage: 8e1f7543's blocking finding cited locations that do not
# exist (`review_citation_demoted`), and 872407d4 took a terminal FAIL on a
# `try/except` the reviewer had not read. Same class as the planner's
# error-string-as-a-plan (1bb3be36).


class _TruncatedBackend:
    """A session that emits a verdict mid-flight and then dies on max_turns.

    Both places `_review_once` looks for a verdict are covered: `final_text`
    (``where="final"``) and the captured event stream it falls back to
    (``where="events"``) — the shape the live incidents had, since the SDK
    replaces `final_text` with its own error sentence.
    """

    def __init__(self, *rounds, where="events"):
        self._rounds = list(rounds)
        self._where = where
        self.budgets: list[int] = []

    async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kw):
        self.budgets.append(max_turns)
        text, errored, usage = self._rounds.pop(0)
        if not errored:
            return _FakeAgentResult(text, **usage)
        if self._where == "events" and on_event is not None:
            on_event(SimpleNamespace(text=text, kind="assistant", meta={}))
        return _FakeAgentResult(
            text if self._where == "final" else _MAX_TURNS_ERROR,
            is_error=True, stop_reason="max_turns", **usage,
        )


# A verdict a truncated session might leave behind — the 872407d4 shape: a
# terminal FAIL on code it had not finished reading.
_BURNED = dict(tokens_used=120_000, cache_read_tokens=900_000,
               cache_creation_tokens=30_000, output_tokens=9_000)
_CHEAP = dict(tokens_used=40_000, cache_read_tokens=50_000,
              cache_creation_tokens=1_000, output_tokens=3_000)

_TRUNCATED_FAIL = (
    'REVIEW_JSON_START {"passed": false, "items": '
    '[{"label": "unverified try/except", "passed": false, "severity": "high", '
    '"evidence": "calc.py:999"}]} REVIEW_JSON_END\n' + _MAX_TURNS_ERROR
)


@pytest.mark.parametrize("where", ["events", "final"])
async def test_a_truncated_reviewer_verdict_is_retried_not_accepted(tmp_path, where):
    """R5. The round did not finish, so its verdict is discarded and the
    EXISTING doubling retry — built for exactly this and unreachable for it —
    runs. Round two's real verdict is what the gate returns."""
    from no_human.review.reviewer import AdversarialReviewer

    backend = _TruncatedBackend(
        (_TRUNCATED_FAIL, True, _BURNED), (_VERDICT, False, _CHEAP), where=where,
    )
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)

    assert len(backend.budgets) == 2, (
        "the truncated round was accepted as a verdict — no retry happened")
    assert backend.budgets[1] > backend.budgets[0], "the retry gets more turns"
    assert decision.passed is True, "the truncated FAIL survived into the verdict"
    assert not any("try/except" in i.label for i in decision.checklist), (
        "a finding from the discarded truncated round leaked into the decision")
    # The round was DISCARDED, not free. Reviewer spend has one channel to the
    # attempt row — the four fields on the decision — so a discarded round that
    # is not folded in here is billed and never recorded, and it is the
    # expensive round: it read the whole diff for 30 turns before dying.
    assert decision.tokens_used == _BURNED["tokens_used"] + _CHEAP["tokens_used"]
    assert decision.cache_read_tokens == (
        _BURNED["cache_read_tokens"] + _CHEAP["cache_read_tokens"])
    assert decision.cache_creation_tokens == (
        _BURNED["cache_creation_tokens"] + _CHEAP["cache_creation_tokens"])
    assert decision.output_tokens == (
        _BURNED["output_tokens"] + _CHEAP["output_tokens"])


@pytest.mark.parametrize("where", ["events", "final"])
async def test_two_truncated_rounds_take_the_existing_no_verdict_path(tmp_path, where):
    """Retry exhaustion is unchanged: ReviewerUnavailable, which the
    orchestrator escalates. No path here turns a truncated verdict into a pass
    — and none of it blames the coder for a finding never properly made."""
    from no_human.review.reviewer import AdversarialReviewer

    backend = _TruncatedBackend(
        (_TRUNCATED_FAIL, True, _BURNED), (_TRUNCATED_FAIL, True, _CHEAP),
        where=where,
    )
    with pytest.raises(ReviewerUnavailable, match="no verdict") as exc:
        await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)

    assert len(backend.budgets) == 2, "one bounded infra retry (constraint #4)"
    assert "reviewer session error (max_turns)" in str(exc.value), (
        "the escalation must name the cause it inherited from the errored round")
    # No decision ever leaves this path, so the exception is the only carrier
    # for two fully-paid reviewer rounds. Recording zero here under-counts the
    # lifetime-budget park gate by the whole gate.
    err = exc.value
    assert err.tokens_used == _BURNED["tokens_used"] + _CHEAP["tokens_used"]
    assert err.cache_read_tokens == (
        _BURNED["cache_read_tokens"] + _CHEAP["cache_read_tokens"])
    assert err.cache_creation_tokens == (
        _BURNED["cache_creation_tokens"] + _CHEAP["cache_creation_tokens"])
    assert err.output_tokens == _BURNED["output_tokens"] + _CHEAP["output_tokens"]


async def test_a_clean_round_is_untouched_by_the_error_gate(tmp_path):
    """Negative control (green before AND after): a non-error result whose
    verdict only appears in the EVENT stream — the fallback parse — still
    returns on round one, with no retry."""
    from no_human.review.reviewer import AdversarialReviewer

    class _EventOnlyBackend:
        def __init__(self):
            self.budgets: list[int] = []

        async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kw):
            self.budgets.append(max_turns)
            on_event(SimpleNamespace(text=_VERDICT, kind="assistant", meta={}))
            return _FakeAgentResult("")

    backend = _EventOnlyBackend()
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert decision.passed is True
    assert len(backend.budgets) == 1, "a healthy round must never be retried"


async def test_tamper_adjudication_fails_closed_on_a_truncated_session(tmp_path):
    """Pins the OTHER mode's behaviour, which does NOT go through
    `_review_once`: `tamper_adjudication` is single-turn via `_fast_review`,
    with one bounded retry when the judge never spoke at all (see
    `test_tamper_adjudication.py`'s retry-contract tests). This adjudicator
    dies the same mechanical way on both the first call and the retry, so it
    still lands on CANNOT_DECIDE — fail-closed by the PARSER, not by the
    retry — so a truncated adjudication cannot pass the tamper gate. Pinned
    so a future edit to either side has to notice."""
    from no_human.review import tamper_adjudication
    from no_human.review.reviewer import AdversarialReviewer

    class _TruncatedAdjudicator:
        async def run(self, prompt, *, cwd, max_turns, effort=None, on_event=None, **kw):
            return _FakeAgentResult(_MAX_TURNS_ERROR, is_error=True,
                                    stop_reason="max_turns")

    decision = await AdversarialReviewer(backend=_TruncatedAdjudicator()).review(
        Task(id="t1", source="manual", title="t", description="d"),
        repo_path=tmp_path, mode="tamper_adjudication",
        tamper_findings="2 assertions deleted", diff_override="- assert x",
    )
    stage = decision.stages[tamper_adjudication.STAGE_KEY]
    assert stage["verdict"] == tamper_adjudication.CANNOT_DECIDE
    assert decision.passed is False, "a truncated adjudication must not clear the guard"


async def test_the_unavailable_reviewer_s_burn_reaches_the_attempt_row():
    """The other half of the same channel: `_record_review_usage` is what the
    orchestrator calls on BOTH exits, so an escalated review gate writes the
    same four columns a verdict would. Before, the `ReviewerUnavailable` branch
    returned before any recording and two paid rounds vanished."""
    from no_human.review.reviewer import ReviewerUnavailable

    written: dict = {}

    class _Store:
        async def update_attempt(self, attempt_id, **kw):
            written.update(kw, attempt_id=attempt_id)

    orch = Orchestrator.__new__(Orchestrator)
    orch.store = _Store()

    exc = ReviewerUnavailable("no verdict after 2 rounds")
    exc.tokens_used, exc.cache_read_tokens = 160_000, 950_000
    exc.cache_creation_tokens, exc.output_tokens = 31_000, 12_000
    await orch._record_review_usage(7, exc)

    assert written == {
        "attempt_id": 7,
        "review_tokens_used": 160_000,
        "review_cache_read_tokens": 950_000,
        "review_cache_creation_tokens": 31_000,
        "review_output_tokens": 12_000,
    }

    # A reviewer that reported no usage split writes NULL, not a measured zero.
    written.clear()
    await orch._record_review_usage(7, ReviewerUnavailable("no reviewer wired"))
    assert written["review_output_tokens"] is None
    assert written["review_tokens_used"] == 0


def test_every_reviewer_unavailable_handler_records_the_burn():
    """The escalation returns BEFORE the orchestrator's normal recording, so
    each `except ReviewerUnavailable` is its own chance to lose a fully-paid
    review gate — and both handlers could be deleted with the whole suite still
    green. Structural, in the shape this file already uses for the gate wiring
    above: the e2e in test_e2e_orchestrator proves the already-satisfied
    handler end to end, and this proves NEITHER handler can quietly stop
    recording (nor a third one arrive without it).

    R17 added the third: `code_review`, which books the reviewer's spend to its
    OWN columns rather than the `review_` ones (that task kind's only spend IS
    the reviewer's), so the guard accepts either channel — `_record_review_usage`
    or an `update_attempt` that carries the usage — and still fails on a handler
    that records through neither.

    The fourth is `tamper_adjudication`'s own `diff_override` caller
    (`_adjudicate_tamper`), the sibling R17 declared and deferred: same
    `_record_review_usage` channel as the gate and already-satisfied handlers.
    """
    src = pathlib.Path("src/no_human/core/orchestrator.py")
    handlers, silent = 0, []
    for node in ast.walk(ast.parse(src.read_text())):
        if not (isinstance(node, ast.ExceptHandler)
                and getattr(node.type, "id", "") == "ReviewerUnavailable"):
            continue
        # A handler that re-raises is not an exit — its caller still records.
        # The ones that RETURN are the last chance the burn has.
        if not any(isinstance(n, ast.Return) for n in ast.walk(node)):
            continue
        handlers += 1
        records = False
        for c in ast.walk(node):
            if not isinstance(c, ast.Call):
                continue
            attr = getattr(c.func, "attr", "")
            if attr == "_record_review_usage" or (
                attr == "update_attempt"
                and any(kw.arg == "tokens_used" for kw in c.keywords)
            ):
                records = True
        if not records:
            silent.append(f"{src}:{node.lineno}")
    assert handlers == 4, (
        f"expected the gate, already-satisfied, code_review and "
        f"tamper_adjudication handlers, found {handlers} — if the path moved, "
        f"this guard is watching nothing")
    assert not silent, (
        "a review gate that escalated still BILLED for its rounds; this handler "
        "returns without recording them: " + ", ".join(silent)
    )


# ------ R17: a MALFORMED verdict is a no-verdict round, not a coder finding -- #
#
# Live funnel killer (2026-08-09, four of five dogfood tasks). Two distinct
# holes, same consequence — the reviewer failing to produce a verdict was
# charged to the CODER and burned one of its three bounded attempts:
#
#   1. `_parse_review_output`'s JSONDecodeError branch labelled the round
#      "json parse", and `_reached_no_verdict` matches only the OTHER label, so
#      a PRESENT-but-malformed verdict walked past the retry/escalation
#      machinery. Task fef3221f, attempts 1 and 2: "review failed: json parse:
#      Expecting ',' delimiter: line 27 column 3 (char 4542)" / "(char 4114)".
#   2. `stop_reason` truncation arriving on a NORMAL ResultMessage (not the
#      terminal-exception path) has `is_error=False`, so R5's hoisted error
#      gate in `_review_once` never saw it and the cut-off text went to the
#      parser. Same known gap the planner's R3 comment records.


def _truncated_verdict_with_end() -> str:
    """The live shape: a long verdict cut mid-item, END marker still present.

    `_REVIEW_JSON` therefore MATCHES (so `_recover_unterminated_verdict` never
    runs) and `loads_lenient` raises — the exact route to the "json parse"
    branch, at the same ~4kB scale as the two live failures.
    """
    items = ",\n".join(
        f'  {{"label": "criterion {n}", "passed": true, "severity": "low", '
        f'"evidence": "calc.py:{n} — verified by reading the function"}}'
        for n in range(1, 26)
    )
    return (
        "REVIEW_JSON_START\n"
        '{"passed": true, "items": [\n' + items
        + ',\n  {"label": "criterion 26", "passed": tr'  # cut mid-token
        + "\nREVIEW_JSON_END"
    )


def test_the_truncated_fixture_really_is_the_live_json_parse_shape():
    """Control on the fixture itself: without it the tests below could pass by
    exercising the (already-handled) missing-END path instead."""
    import json as _json

    from no_human.core.jsonparse import loads_lenient
    from no_human.review.reviewer import _REVIEW_JSON

    m = _REVIEW_JSON.search(_truncated_verdict_with_end())
    assert m, "the END marker must be present or this is the par-07 path"
    with pytest.raises(_json.JSONDecodeError, match="Expecting"):
        loads_lenient(m.group(1))


def test_a_malformed_verdict_is_the_no_verdict_sentinel(tmp_path):
    """(a) One shape for one event. A block that is present but does not parse
    is the gate failing to run, exactly like a block that is absent."""
    from no_human.review.reviewer import _parse_review_output, _reached_no_verdict

    d = _parse_review_output(_truncated_verdict_with_end())
    assert d.passed is False
    assert _reached_no_verdict(d), (
        "a malformed verdict bypassed the no-verdict machinery — this is what "
        "fed the parse exception to the coder as a finding")
    # The diagnosis is not lost, it just stops being a finding about the diff.
    assert "Expecting" in d.checklist[0].evidence


async def test_a_malformed_verdict_is_retried_then_escalated(tmp_path):
    """(a) end to end on the gate path: retry at a doubled budget, and on a
    second malformed round, ReviewerUnavailable naming the reviewer — never a
    checklist item the coder is told to fix."""
    from no_human.review.reviewer import AdversarialReviewer

    broken = _truncated_verdict_with_end()
    backend = _ReviewerBackend(
        _FakeAgentResult(broken, stop_reason="end_turn"),
        _FakeAgentResult(_VERDICT, stop_reason="end_turn"),
    )
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert decision.passed is True
    assert len(backend.budgets) == 2 and backend.budgets[1] > backend.budgets[0]
    assert not any("json parse" in i.label or "Expecting" in i.evidence
                   for i in decision.checklist), "the parse error reached the coder"

    twice = _ReviewerBackend(
        _FakeAgentResult(broken, stop_reason="end_turn"),
        _FakeAgentResult(broken, stop_reason="end_turn"),
    )
    with pytest.raises(ReviewerUnavailable, match="the reviewer reached no verdict"):
        await AdversarialReviewer(backend=twice)._agent_review("p", tmp_path)


async def test_max_turns_on_a_normal_result_is_a_no_verdict_round(tmp_path):
    """(b) The R3 known gap, on the reviewer. `is_error` is False on the normal
    ResultMessage path, so R5's error gate missed it and the truncated text was
    parsed as a verdict. The doubling retry exists for exactly this."""
    from no_human.review.reviewer import AdversarialReviewer

    backend = _ReviewerBackend(
        _FakeAgentResult(_truncated_verdict_with_end(),
                         is_error=False, stop_reason="max_turns"),
        _FakeAgentResult(_VERDICT, stop_reason="end_turn"),
    )
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert decision.passed is True
    assert len(backend.budgets) == 2, "the truncated normal-path round was accepted"
    assert backend.budgets[1] > backend.budgets[0], "the retry gets more turns"


async def test_a_complete_verdict_from_a_cut_off_round_is_still_discarded(tmp_path):
    """(b), the other half: a round that ran out of turns did not FINISH, so
    even a parseable verdict in it is a mid-exploration state of mind (R5's
    rule) — the normal-path shape must be treated like the exception path."""
    from no_human.review.reviewer import AdversarialReviewer

    terminal_fail = (
        'REVIEW_JSON_START {"passed": false, "items": '
        '[{"label": "unread try/except", "passed": false, "severity": "high", '
        '"evidence": "calc.py:999"}]} REVIEW_JSON_END'
    )
    backend = _ReviewerBackend(
        _FakeAgentResult(terminal_fail, is_error=False, stop_reason="max_turns"),
        _FakeAgentResult(_VERDICT, stop_reason="end_turn"),
    )
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert len(backend.budgets) == 2, "a cut-off round's verdict was taken at face value"
    assert decision.passed is True
    assert not any("try/except" in i.label for i in decision.checklist)


async def test_an_angle_that_reaches_no_verdict_never_fails_the_gate(tmp_path):
    """(c) FINDING 3, the path that let the no-verdict label through as an
    ATTEMPT-FAIL on R5 code (task 87fcf4eb, attempts 1 AND 2, complex tier).

    The gate review is intercepted in `_agent_review`; the complex-tier ANGLE
    passes are not — they run through `_fast_review`, and `merge_angle_findings`
    appends any failed item, which for a no-verdict angle is the fail-closed
    sentinel with no severity. Unclassified ⇒ blocking ⇒ a PASSING gate flips to
    FAIL with "structured output present: reviewer produced no parseable
    REVIEW_JSON block" as the finding the coder is told to fix. Angles are
    advisory by contract — one that did not run is a note, never a verdict.
    """
    from no_human.core.task import Task
    from no_human.review.reviewer import AdversarialReviewer

    broken = _truncated_verdict_with_end()

    class _MainPassesAnglesBreak:
        def __init__(self):
            self.n = 0

        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      on_event=None, **kw):
            self.n += 1
            if self.n == 1:
                return _FakeAgentResult(_VERDICT, stop_reason="end_turn")
            return _FakeAgentResult(broken, stop_reason="end_turn")

    backend = _MainPassesAnglesBreak()
    task = Task.new("big task", repo_path=str(tmp_path))
    task.context = {"complexity_tier": "complex"}
    d = await AdversarialReviewer(backend=backend).review(
        task, repo_path=tmp_path, diff_override="+ x = 1\n")

    assert backend.n == 5, "main + 4 angles"
    assert d.passed is True, "an angle that never reached a verdict failed the gate"
    notes = [i for i in d.checklist if "did not run" in i.label]
    assert len(notes) == 4 and all(i.passed for i in notes)
    assert not any("no parseable REVIEW_JSON" in i.evidence for i in d.checklist), (
        "the reviewer's own failure was appended as a finding against the diff")


async def test_a_single_turn_gate_review_with_no_verdict_escalates(tmp_path):
    """(c), the sibling hole: the gate's `diff_override` path also returns
    `_fast_review`'s decision straight out of `review()`. A no-verdict there is
    the gate failing to run, so it escalates like every other gate path instead
    of failing the attempt."""
    from no_human.core.task import Task
    from no_human.review.reviewer import AdversarialReviewer

    class _BrokenOnce:
        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      on_event=None, **kw):
            return _FakeAgentResult(_truncated_verdict_with_end(),
                                    stop_reason="end_turn")

    task = Task.new("small task", repo_path=str(tmp_path))
    task.context = {"complexity_tier": "simple"}
    with pytest.raises(ReviewerUnavailable, match="no verdict"):
        await AdversarialReviewer(backend=_BrokenOnce()).review(
            task, repo_path=tmp_path, diff_override="+ x = 1\n")


async def test_tamper_adjudication_still_cannot_decide_on_garbage(tmp_path):
    """Control for (a): the tamper adjudicator reads its verdict from its OWN
    parser and overwrites the checklist, so relabelling the parse failure must
    not change it — garbage still parks, and still does not clear the guard."""
    from no_human.review import tamper_adjudication
    from no_human.review.reviewer import AdversarialReviewer

    class _Garbage:
        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      on_event=None, **kw):
            return _FakeAgentResult(_truncated_verdict_with_end(),
                                    stop_reason="end_turn")

    decision = await AdversarialReviewer(backend=_Garbage()).review(
        Task(id="t1", source="manual", title="t", description="d"),
        repo_path=tmp_path, mode="tamper_adjudication",
        tamper_findings="2 assertions deleted", diff_override="- assert x",
    )
    stage = decision.stages[tamper_adjudication.STAGE_KEY]
    assert stage["verdict"] == tamper_adjudication.CANNOT_DECIDE
    assert decision.passed is False


async def test_a_truncation_by_OUTPUT_LENGTH_is_also_a_no_verdict_round(tmp_path):
    """(b), the half the live incident actually was: a verdict cut mid-JSON at
    char 4542 is an OUTPUT-length cut, so the session ends NORMALLY —
    `is_error` False, `stop_reason` "max_tokens" — and `max_tokens` carries the
    fix. It must be pinned by a round whose verdict PARSES, or the assertion
    only re-proves the parser fix and stays green with "max_tokens" mutated out
    of `_TRUNCATED_STOP_REASONS` (it did).

    So: a complete, parseable FAIL emitted by a session that was then cut off.
    R5's rule is that a round which did not finish has no conclusion to read —
    that terminal FAIL is a mid-write state of mind, and taking it at face
    value is what charges the coder for a finding never properly made."""
    from no_human.review.reviewer import AdversarialReviewer

    cut_off_fail = (
        'REVIEW_JSON_START {"passed": false, "items": '
        '[{"label": "unread try/except", "passed": false, "severity": "high", '
        '"evidence": "calc.py:999"}]} REVIEW_JSON_END'
    )
    backend = _ReviewerBackend(
        _FakeAgentResult(cut_off_fail, is_error=False, stop_reason="max_tokens"),
        _FakeAgentResult(_VERDICT, stop_reason="end_turn"),
    )
    decision = await AdversarialReviewer(backend=backend)._agent_review("p", tmp_path)
    assert len(backend.budgets) == 2, "an output-truncated round was read as a verdict"
    assert backend.budgets[1] > backend.budgets[0], "the retry gets more turns"
    assert decision.passed is True
    assert not any("try/except" in i.label for i in decision.checklist), (
        "the cut-off round's finding was charged to the coder")


async def test_a_skipped_angle_still_bills_its_tokens_to_the_attempt(tmp_path):
    """R5's accounting discipline, on the new skip path. The advisory `continue`
    walks past `merge_angle_findings` — the ONLY fold of an angle's usage — and
    `_fast_review` stamps real spend on the decision it returns. Without the
    fold, three paid Opus angle sessions bill and report nothing, which
    under-counts the lifetime-budget park gate by the whole angle fan-out."""
    from no_human.core.task import Task
    from no_human.review.reviewer import AdversarialReviewer

    broken = _truncated_verdict_with_end()
    spend = dict(tokens_used=50_000, cache_read_tokens=400_000,
                 cache_creation_tokens=7_000, output_tokens=2_000)

    class _AnglesBurnThenBreak:
        n = 0

        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      on_event=None, **kw):
            type(self).n += 1
            if self.n == 1:
                return _FakeAgentResult(_VERDICT, stop_reason="end_turn",
                                        tokens_used=1_000, output_tokens=100)
            return _FakeAgentResult(broken, stop_reason="end_turn", **spend)

    task = Task.new("big task", repo_path=str(tmp_path))
    task.context = {"complexity_tier": "complex"}
    d = await AdversarialReviewer(backend=_AnglesBurnThenBreak()).review(
        task, repo_path=tmp_path, diff_override="+ x = 1\n")

    assert d.passed is True
    assert d.tokens_used == 1_000 + 4 * spend["tokens_used"], (
        "four skipped angles were billed and reported nothing")
    assert d.cache_read_tokens == 4 * spend["cache_read_tokens"]
    assert d.cache_creation_tokens == 4 * spend["cache_creation_tokens"]
    assert d.output_tokens == 100 + 4 * spend["output_tokens"]


async def test_the_escalating_single_turn_gate_carries_its_burn(tmp_path):
    """Same discipline on the other new exit: the round that reached no verdict
    was PAID, and the exception is the only thing that leaves this path."""
    from no_human.core.task import Task
    from no_human.review.reviewer import AdversarialReviewer

    class _BrokenOnce:
        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      on_event=None, **kw):
            return _FakeAgentResult(_truncated_verdict_with_end(),
                                    stop_reason="end_turn", tokens_used=80_000,
                                    cache_read_tokens=500_000,
                                    cache_creation_tokens=9_000,
                                    output_tokens=4_000)

    task = Task.new("small task", repo_path=str(tmp_path))
    task.context = {"complexity_tier": "simple"}
    with pytest.raises(ReviewerUnavailable) as exc:
        await AdversarialReviewer(backend=_BrokenOnce()).review(
            task, repo_path=tmp_path, diff_override="+ x = 1\n")

    assert exc.value.tokens_used == 80_000
    assert exc.value.cache_read_tokens == 500_000
    assert exc.value.cache_creation_tokens == 9_000
    assert exc.value.output_tokens == 4_000


async def test_a_code_review_task_books_an_unavailable_reviewer_s_burn(
    bare_repo, tmp_path, store, monkeypatch
):
    """R17/F3 — the carried burn needs a RECORDER, and this site is the one
    place `_record_review_usage` is not it.

    Routing a malformed verdict to the no-verdict retry made
    `ReviewerUnavailable` reachable on the `code_review` path, where the only
    handler is a bare `except Exception` that records nothing — so a fully paid
    Opus round bills 0. Pre-R17 the garbled round returned a STAMPED decision
    and the `update_attempt` after the try booked it, which makes this a
    regression against 97596a7e, not just an old gap.

    The columns are this site's own (`tokens_used`, not `review_tokens_used`):
    a code_review task's ONLY spend is the reviewer's, and the site books it to
    the coder-tier columns deliberately — see the comment there.
    """
    from no_human.core.orchestrator import Orchestrator

    class _UnavailableReviewer:
        model = "claude-opus-5"
        _on_event = None

        async def review(self, *a, **kw):
            exc = ReviewerUnavailable(
                "the reviewer reached no verdict after 2 rounds")
            exc.tokens_used, exc.cache_read_tokens = 120_000, 900_000
            exc.cache_creation_tokens, exc.output_tokens = 30_000, 9_000
            raise exc

    orch = Orchestrator(store, _config(tmp_path).data,
                        FakeBackend(lambda cwd: None), SlackNotifier(None),
                        reviewer=_UnavailableReviewer())
    t = Task.new("review this PR https://forge.example/x/y/pull/42",
                 repo_path=str(bare_repo), kind="code_review")
    await store.create_task(t)
    monkeypatch.setattr(orch, "_fetch_pr_diff",
                        lambda repo, url: "diff --git a/f.py b/f.py\n+x = 1\n")

    async def _no_comments(pr_url):
        return ""
    monkeypatch.setattr(orch, "_fetch_pr_comments_text", _no_comments)

    outcome = await orch.run_task(t)

    assert outcome.status is TaskStatus.FAILED, "the gate did not run — fail closed"
    (attempt,) = await store.list_attempts(t.id)
    assert attempt["tokens_used"] == 120_000, "a paid reviewer round billed 0"
    assert attempt["cache_read_tokens"] == 900_000
    assert attempt["cache_creation_tokens"] == 30_000
    assert attempt["output_tokens"] == 9_000


# ------ the sibling hole: `tamper_adjudication`'s own diff_override caller -- #


async def test_a_tamper_adjudication_books_an_unavailable_reviewer_s_burn(
    bare_repo, tmp_path, store
):
    """The declared-and-deferred sibling of R17/F3: this call site's only
    handler was a bare `except Exception` that recorded nothing, so a fully
    paid adjudication round billed 0. Mirrors R17's cure, but on THIS site's
    own channel — `_record_review_usage` / the `review_*` columns, not the
    coder-tier ones `_adjudicate_tamper` runs inside of (see the comment at
    the call site for why: `tokens_used` already holds the CODER's burn)."""
    from no_human.core.orchestrator import Orchestrator

    class _UnavailableAdjudicator:
        model = "claude-opus-5"
        _on_event = None

        async def review(self, *a, **kw):
            exc = ReviewerUnavailable(
                "the reviewer reached no verdict after 2 rounds")
            exc.tokens_used, exc.cache_read_tokens = 120_000, 900_000
            exc.cache_creation_tokens, exc.output_tokens = 30_000, 9_000
            raise exc

    orch = Orchestrator(store, _config(tmp_path).data,
                        FakeBackend(lambda cwd: None), SlackNotifier(None),
                        reviewer=_UnavailableAdjudicator())
    assert orch._tamper_adjudication_enabled() is True, (
        "an accidentally-disabled config would exercise nothing")

    task = Task.new("add add()", repo_path=str(bare_repo))
    task.acceptance_criteria = ["add(a,b) returns a+b"]
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    report = SimpleNamespace(
        tampered=True, reasons=["tests/test_calc.py: tests 2->1"],
        summary="a test was deleted")

    outcome = await orch._handle_tamper_fire(
        task, report, repo=None, branch="nh/x", attempt_id=attempt_id,
        attempt_n=1, diff_repo=bare_repo, before_ref="HEAD~1")

    assert outcome is not None, "CANNOT_DECIDE must stop the run, not continue it"
    (attempt,) = await store.list_attempts(task.id)
    assert attempt["review_tokens_used"] == 120_000, (
        "a paid adjudication round billed 0")
    assert attempt["review_cache_read_tokens"] == 900_000
    assert attempt["review_cache_creation_tokens"] == 30_000
    assert attempt["review_output_tokens"] == 9_000


async def test_a_tamper_adjudication_with_no_reported_split_writes_null(
    bare_repo, tmp_path, store
):
    """Same channel, the `None -> NULL` half: an absent usage split must stay
    distinguishable from a measured zero all the way to the column."""
    from no_human.core.orchestrator import Orchestrator

    class _UnavailableAdjudicator:
        model = "claude-opus-5"
        _on_event = None

        async def review(self, *a, **kw):
            raise ReviewerUnavailable("no reviewer session ever started")

    orch = Orchestrator(store, _config(tmp_path).data,
                        FakeBackend(lambda cwd: None), SlackNotifier(None),
                        reviewer=_UnavailableAdjudicator())
    task = Task.new("add add()", repo_path=str(bare_repo))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    await orch._handle_tamper_fire(
        task, SimpleNamespace(tampered=True, reasons=["x"], summary="s"),
        repo=None, branch="nh/x", attempt_id=attempt_id, attempt_n=1,
        diff_repo=bare_repo, before_ref="HEAD~1")

    (attempt,) = await store.list_attempts(task.id)
    assert attempt["review_output_tokens"] is None
    assert attempt["review_tokens_used"] == 0


async def test_a_real_adjudicator_s_burn_is_booked_on_the_path_it_takes(
    bare_repo, tmp_path, store
):
    """The path production ACTUALLY takes: `mode="tamper_adjudication"` never
    raises `ReviewerUnavailable` (see
    `test_tamper_adjudication_fails_closed_on_a_truncated_session` — it is
    fail-closed by its own PARSER, not by the no-verdict retry machinery), so
    the `except ReviewerUnavailable` handler alone would be dead code against
    the real reviewer. A garbled single-turn round still returns a decision —
    CANNOT_DECIDE, and still paid for — and that decision's usage must be
    booked here, on the branch that runs every time."""
    from no_human.review import tamper_adjudication
    from no_human.review.reviewer import AdversarialReviewer

    class _Garbage:
        async def run(self, prompt, *, cwd, max_turns, effort=None,
                      on_event=None, **kw):
            return _FakeAgentResult(
                _truncated_verdict_with_end(), stop_reason="end_turn",
                tokens_used=45_000, cache_read_tokens=300_000,
                cache_creation_tokens=6_000, output_tokens=1_500,
            )

    orch = Orchestrator(store, _config(tmp_path).data,
                        FakeBackend(lambda cwd: None), SlackNotifier(None),
                        reviewer=AdversarialReviewer(backend=_Garbage()))
    task = Task.new("add add()", repo_path=str(bare_repo))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)

    adj = await orch._adjudicate_tamper(
        task, SimpleNamespace(tampered=True, reasons=["x"], summary="s"),
        diff_repo=bare_repo, before_ref="HEAD~1", attempt_id=attempt_id)

    assert adj.verdict == tamper_adjudication.CANNOT_DECIDE
    (attempt,) = await store.list_attempts(task.id)
    assert attempt["review_tokens_used"] == 45_000, (
        "a paid adjudication round that returned a decision (no raise) billed 0")
    assert attempt["review_cache_read_tokens"] == 300_000
    assert attempt["review_cache_creation_tokens"] == 6_000
    assert attempt["review_output_tokens"] == 1_500


async def test_an_unavailable_adjudicator_still_parks_exactly_as_before(
    bare_repo, tmp_path, store
):
    """AC2: the new recording must not change a single byte of the failure
    path. Same status, same failure-reason shape, same blocker, same advisory
    wording, same context entry — and the CODER's own `tokens_used` (a
    different column, pre-set on the row before this call) must survive
    untouched, which is the anti-clobber half of the guarantee."""
    from no_human.core.orchestrator import Orchestrator

    events = []

    class _UnavailableAdjudicator:
        model = "claude-opus-5"
        _on_event = None

        async def review(self, *a, **kw):
            exc = ReviewerUnavailable("the reviewer reached no verdict")
            exc.tokens_used = 50_000
            raise exc

    orch = Orchestrator(store, _config(tmp_path).data,
                        FakeBackend(lambda cwd: None), SlackNotifier(None),
                        reviewer=_UnavailableAdjudicator(),
                        event_sink=events.append)
    task = Task.new("add add()", repo_path=str(bare_repo))
    await store.create_task(task)
    attempt_id = await store.create_attempt(task.id, 1)
    await store.update_attempt(attempt_id, tokens_used=7_777)

    report = SimpleNamespace(
        tampered=True, reasons=["tests/test_calc.py: tests 2->1"],
        summary="a test was deleted")

    outcome = await orch._handle_tamper_fire(
        task, report, repo=None, branch="nh/x", attempt_id=attempt_id,
        attempt_n=1, diff_repo=bare_repo, before_ref="HEAD~1")

    assert outcome.status is TaskStatus.AWAITING_INPUT, "CANNOT_DECIDE parks"
    (attempt,) = await store.list_attempts(task.id)
    assert attempt["status"] == "failed"
    assert "CANNOT_DECIDE" in attempt["failure_reason"]
    assert attempt["tokens_used"] == 7_777, (
        "the coder's own spend must not be clobbered by the reviewer's column")

    blocker = (await store.get_task(task.id)).blocker
    assert blocker is not None, "CANNOT_DECIDE must reach a human"

    ctx = await store.merge_context(task.id, {})
    entry = ctx["tamper_adjudications"][-1]
    assert "the tamper adjudication could not run" in entry["uncertainty"]

    (advisory,) = [e for e in events if e["kind"] == "advisory"]
    assert "tamper adjudication failed to run" in advisory["text"]
