"""`LandedClaimGuardHook` warns a coder mid-attempt, the moment its own
"already landed" claim is one delivery will go on to refuse, instead of
letting the attempt burn its whole turn budget only to be refused at the
real claim gate. It holds no opinion of its own: every predicate behind the
answer lives in `Orchestrator.claim_gate_decision`, and this file's job is
to prove the guard never grows a second copy of any of them, stays silent
whenever delivery would not yet refuse (or could not be sure), and only
ever surfaces delivery's own reason, word for word.
"""

from __future__ import annotations

import ast
import asyncio
import re
import subprocess
import threading
from functools import partial
from pathlib import Path

import pytest

import no_human.vcs.git as git_module
from no_human.agent.landed_claim_guard import LandedClaimGuardHook
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task
from no_human.notify.slack import SlackNotifier
from no_human.vcs import GitRepo

CLAIM_TEXT = (
    "ALREADY-SATISFIED\n"
    "CRITERION: existing — MET — evidence: calc.py:1\n"
)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def bare_repo(tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "u@example.test")
    _git(work, "config", "user.name", "u")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return work


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("blockers", {})["challenge"] = False
    return cfg


class _NullBackend:
    """`claim_gate_decision` never touches the backend — these tests drive
    it directly through the guard, so any placeholder satisfies the
    constructor."""


def _orch(store, tmp_path):
    return Orchestrator(store, _config(tmp_path).data, _NullBackend(),
                        SlackNotifier(None))


def _events(orch):
    """Record `(kind, text)` pairs `orch.emit` is called with, without
    needing a real event bus."""
    fired = []
    orch.emit = lambda kind, text="", **kw: fired.append((kind, text))
    return fired


def _make_task(kind="feature"):
    task = Task.new("t", kind=kind)
    task.acceptance_criteria = ["existing"]
    return task


def _hook(orch, task, repo, *, base, branch, branched_from_own_partial=False):
    return LandedClaimGuardHook(
        decide=partial(
            orch.claim_gate_decision, task, repo,
            base=base, branch=branch,
            branched_from_own_partial=branched_from_own_partial,
            announce=False,
        ),
        on_event=orch.emit,
    )


async def _drive(hook) -> str:
    """Drive a `LandedClaimGuardHook` to settlement without ever awaiting
    its private `_task` directly — the same shape a real PostToolUse loop
    uses: call `hook()` repeatedly, yielding the loop between calls, until
    it stops returning `{}` for lack of a resolved background check."""
    for _ in range(10_000):
        result = await hook.hook({}, None, None)
        if result:
            ctx = result.get("hookSpecificOutput", {})
            return ctx.get("additionalContext", "")
        if hook._done:
            return ""
        await asyncio.sleep(0)
    raise AssertionError("guard never settled — background check stuck")


def _sibling_ahead_shape(bare_repo, tmp_path, stem):
    """A repo where `{stem}-2` (never pushed under that name) is the branch
    on offer, and a sibling `{stem}` — pushed from a SEPARATE clone so this
    tree's local object store never sees its tip — sits one commit ahead of
    it on the remote, containing HEAD as an ancestor. Resolving that
    containment forces exactly the fetch `_have_remote_commit` performs on
    a cache miss, the path AC4 requires stay safe under failure."""
    branch = f"{stem}-2"
    _git(bare_repo, "checkout", "-b", branch)
    (bare_repo / "work.txt").write_text("work\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    head = GitRepo(bare_repo).head_sha()

    # Build the one-ahead commit in a clone of `bare_repo` ITSELF (which can
    # see `head`, never pushed to origin), then push straight to the real
    # origin under `stem` — `bare_repo`'s own local object store never
    # learns of this commit, so resolving it forces the fetch
    # `_have_remote_commit` performs on a cache miss.
    clone2 = tmp_path / "clone2"
    _git(tmp_path, "clone", str(bare_repo), str(clone2))
    _git(clone2, "config", "user.email", "u@example.test")
    _git(clone2, "config", "user.name", "u")
    _git(clone2, "checkout", branch)
    (clone2 / "more.txt").write_text("more\n")
    _git(clone2, "add", "-A")
    _git(clone2, "commit", "-m", "more")
    origin_url = _git(bare_repo, "remote", "get-url", "origin").stdout.strip()
    _git(clone2, "push", origin_url, f"HEAD:refs/heads/{stem}")
    return branch, head


# ---------------------------------------------------------------------------
# AC2 — silence when delivery's own decision would not even reach the claim
# gate.
# ---------------------------------------------------------------------------


async def test_silent_when_the_tree_has_uncommitted_changes(bare_repo, tmp_path, store):
    """Repo otherwise in the exact state delivery would refuse a claim in —
    plus one uncommitted file. `""` and no event: the dirty tree ends the
    decision before a claim is even possible. **AC5 mutation canary**: stub
    `claim_gate_decision`'s `has_changes()` read to `dirty = False` and this
    is the test that goes red."""
    orch = _orch(store, tmp_path)
    fired = _events(orch)
    task = _make_task()
    stem = task.id[:8]
    _git(bare_repo, "checkout", "-b", stem)
    (bare_repo / "committed.py").write_text("x = 1\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    # This tree, once the uncommitted edit below is set aside, is in the
    # exact state `test_refuses_when_delivery_would_refuse` shows delivery
    # refusing — so if `has_changes()` stopped being read, this test would
    # start seeing that refusal instead of staying silent.
    (bare_repo / "calc.py").write_text("def add(a, b):\n    return a + b + 0\n")

    hook = _hook(orch, task, GitRepo(bare_repo), base=stem, branch=stem)
    hook.note_text(CLAIM_TEXT)
    injected = await _drive(hook)

    assert injected == ""
    assert fired == []


async def test_silent_for_a_report_kind_task(bare_repo, tmp_path, store):
    """Same refusing state (clean tree, nothing pushed), but an
    investigation/design-doc task — proving the report-kind gate, not the
    tree, is what produced the silence."""
    orch = _orch(store, tmp_path)
    fired = _events(orch)
    task = _make_task(kind="investigation")
    stem = task.id[:8]
    _git(bare_repo, "checkout", "-b", stem)

    hook = _hook(orch, task, GitRepo(bare_repo), base=stem, branch=stem)
    hook.note_text("already landed at " + GitRepo(bare_repo).head_sha())
    injected = await _drive(hook)

    assert injected == ""
    assert fired == []


async def test_silent_when_the_branch_has_commits_ahead_of_base(bare_repo, tmp_path, store):
    """A branch with a real commit ahead of `base` that no completed review
    judged routes to a full review, not the claim gate — the guard must
    stay silent rather than pre-empt that review."""
    orch = _orch(store, tmp_path)
    fired = _events(orch)
    task = _make_task()
    stem = task.id[:8]
    _git(bare_repo, "checkout", "-b", stem)
    (bare_repo / "more.py").write_text("x = 1\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "wip")

    hook = _hook(orch, task, GitRepo(bare_repo), base="main", branch=stem,
                branched_from_own_partial=False)
    hook.note_text(CLAIM_TEXT)
    injected = await _drive(hook)

    assert injected == ""
    assert fired == []


async def test_an_own_partial_resume_is_not_silenced_by_commits_ahead(bare_repo, tmp_path, store):
    """Control for the previous test: with `branched_from_own_partial=True`
    the decision does not stop at the commits-ahead check — it proceeds
    (here, on to a clean subject tree — nothing pushed under this branch's
    own name — and refuses). Proves the previous test's silence really came
    from the resumed-branch step, not from something else."""
    orch = _orch(store, tmp_path)
    task = _make_task()
    stem = task.id[:8]
    _git(bare_repo, "checkout", "-b", stem)
    (bare_repo / "more.py").write_text("x = 1\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "wip")

    repo = GitRepo(bare_repo)
    gate = await orch.claim_gate_decision(
        task, repo, base="main", branch=stem,
        branched_from_own_partial=True, final_text=CLAIM_TEXT, announce=False)

    assert gate.stage == "claim"


# ---------------------------------------------------------------------------
# AC3 — refusal when delivery itself would refuse; silence when it would
# accept via a sibling branch.
# ---------------------------------------------------------------------------


async def test_refuses_when_delivery_would_refuse(bare_repo, tmp_path, store):
    """Clean tree, zero commits ahead of its own branch, HEAD off
    `origin/main`, nothing pushed anywhere. Delivery's own
    `_already_satisfied_subject` refuses this; the guard must inject that
    exact reason, not a guess at it — computed here directly against the
    same repo rather than hardcoded, so this test tracks delivery's wording
    instead of a stale copy of it."""
    orch = _orch(store, tmp_path)
    fired = _events(orch)
    task = _make_task()
    stem = task.id[:8]
    _git(bare_repo, "checkout", "-b", stem)
    (bare_repo / "work.txt").write_text("work\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    repo = GitRepo(bare_repo)

    hook = _hook(orch, task, repo, base=stem, branch=stem)
    hook.note_text(CLAIM_TEXT)
    injected = await _drive(hook)

    subject = await orch._already_satisfied_subject(task, repo, base=stem, branch=stem)
    assert subject[0] is False, "fixture drifted — delivery no longer refuses this state"

    assert "LANDED-CLAIM REFUSED" in injected
    assert subject[3] in injected
    assert [k for k, _ in fired] == ["landed_claim_refused"]


async def test_silent_when_a_sibling_branch_really_contains_head(bare_repo, tmp_path, store):
    """A sibling branch under this task's own name prefix really does
    contain HEAD, pushed to origin — delivery accepts this via that
    sibling. The guard must never refuse what delivery would go on to
    accept."""
    orch = _orch(store, tmp_path)
    fired = _events(orch)
    task = _make_task()
    stem = f"no-human/{task.id[:8]}"
    branch, _head = _sibling_ahead_shape(bare_repo, tmp_path, stem)
    repo = GitRepo(bare_repo)

    subject = await orch._already_satisfied_subject(task, repo, base=branch, branch=branch)
    assert subject[0] is True, "fixture drifted — delivery no longer accepts via the sibling"

    hook = _hook(orch, task, repo, base=branch, branch=branch)
    hook.note_text(CLAIM_TEXT)
    injected = await _drive(hook)

    assert injected == ""
    assert fired == []


# ---------------------------------------------------------------------------
# AC4 — a failed/timed-out ls-remote or fetch anywhere in the decision never
# becomes a definite refusal.
# ---------------------------------------------------------------------------


class _BrokenRun:
    """Wraps `no_human.vcs.git.subprocess.run`, breaking exactly the
    sibling-branch lookup's `ls-remote --heads` or the fetch it triggers on
    a cache miss — everything else (including `remote_branch_relation`'s
    own, unrelated `ls-remote`) passes through untouched."""

    def __init__(self, mode):
        self.mode = mode
        self._real = subprocess.run

    def __call__(self, cmd, *args, **kwargs):
        argv = list(cmd)
        is_sibling_ls_remote = argv[:3] == ["git", "ls-remote", "--heads"]
        is_fetch = len(argv) > 1 and argv[0] == "git" and argv[1] == "fetch"
        if self.mode == "ls_remote_timeout" and is_sibling_ls_remote:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))
        if self.mode == "git_oserror" and is_sibling_ls_remote:
            raise OSError("git executable not found")
        if self.mode == "fetch_rc" and is_fetch:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fetch failed")
        if self.mode == "fetch_timeout" and is_fetch:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))
        return self._real(cmd, *args, **kwargs)


async def test_the_sibling_ahead_shape_accepts_with_no_failure_injected(bare_repo, tmp_path, store):
    """Control for the parametrized failure tests below: with nothing
    broken, the sibling-ahead fixture itself resolves to acceptance — so a
    guard silent under failure for the WRONG reason (e.g. a fixture that
    was already silent) cannot pass those tests unnoticed."""
    orch = _orch(store, tmp_path)
    task = _make_task()
    stem = f"no-human/{task.id[:8]}"
    branch, _head = _sibling_ahead_shape(bare_repo, tmp_path, stem)
    repo = GitRepo(bare_repo)

    subject = await orch._already_satisfied_subject(task, repo, base=branch, branch=branch)
    assert subject[0] is True


@pytest.mark.parametrize(
    "mode", ["ls_remote_timeout", "fetch_rc", "fetch_timeout", "git_oserror"],
)
async def test_a_transient_git_failure_never_becomes_a_refusal(
    bare_repo, tmp_path, store, monkeypatch, mode,
):
    """Breaking the sibling-branch lookup in any of these ways must yield
    `undetermined`, never a refusal — an unproven refusal is worse than no
    warning. `fetch_rc` is the reproduced regression: a failed fetch
    silently dropped a branch that DID contain HEAD, and the guard then
    asserted the claim was never pushed anywhere."""
    orch = _orch(store, tmp_path)
    fired = _events(orch)
    task = _make_task()
    stem = f"no-human/{task.id[:8]}"
    branch, _head = _sibling_ahead_shape(bare_repo, tmp_path, stem)
    repo = GitRepo(bare_repo)

    monkeypatch.setattr(git_module.subprocess, "run", _BrokenRun(mode))

    hook = _hook(orch, task, repo, base=branch, branch=branch)
    hook.note_text(CLAIM_TEXT)
    injected = await _drive(hook)

    assert injected == ""
    assert [k for k, _ in fired] == ["landed_claim_undetermined"]


async def test_a_real_unreachable_remote_never_becomes_a_refusal(bare_repo, tmp_path, store):
    """`ls_remote_rc`: a real, non-mocked failure — the bare remote
    directory is renamed out from under `git`, so every `ls-remote` against
    it fails with a genuine non-zero return code, not a simulated one."""
    orch = _orch(store, tmp_path)
    fired = _events(orch)
    task = _make_task()
    stem = f"no-human/{task.id[:8]}"
    branch, _head = _sibling_ahead_shape(bare_repo, tmp_path, stem)
    repo = GitRepo(bare_repo)

    remote_dir = tmp_path / "remote.git"
    moved = tmp_path / "remote.git.moved"
    remote_dir.rename(moved)
    try:
        hook = _hook(orch, task, repo, base=branch, branch=branch)
        hook.note_text(CLAIM_TEXT)
        injected = await _drive(hook)
    finally:
        moved.rename(remote_dir)

    assert injected == ""
    assert [k for k, _ in fired] == ["landed_claim_undetermined"]


# ---------------------------------------------------------------------------
# Structural: no second copy of delivery's predicates, no leaked history.
# ---------------------------------------------------------------------------


def _guard_src() -> str:
    import no_human.agent.landed_claim_guard as mod
    return Path(mod.__file__).read_text(encoding="utf-8")


def test_the_guard_contains_no_copy_of_the_delivery_predicates():
    """The guard must recognize claim-shaped prose and forward it to
    `decide` — never recompute any predicate `claim_gate_decision` already
    owns. Absence of each name is the assertion; presence of `decide` is
    the positive control proving the scan itself isn't vacuous."""
    src = _guard_src()
    forbidden = [
        "has_changes", "commits_ahead", "_REPORT_KINDS", "head_commit",
        "is_ancestor", "remote_branches_containing", "remote_branch_relation",
        "branch_sha", "_already_satisfied_subject", "_already_satisfied_eligible",
    ]
    for needle in forbidden:
        assert needle not in src, (
            f"{needle!r} found in landed_claim_guard.py — that is a second "
            "copy of a delivery predicate, the exact shape this module "
            "exists to avoid")
    assert "decide" in src, "positive control failed — the scan itself is broken"


def test_shipped_text_carries_no_review_history():
    """Docstrings and comments describe the mechanism, not this task's own
    history: no 8-hex id-shaped token, no `PR #`/`issue #` reference, no
    "round"/"attempt N" phrasing, no bare unexplained figure."""
    src = _guard_src()
    assert not re.search(r"\bPR\s*#\d+", src, re.IGNORECASE)
    assert not re.search(r"\bissue\s*#\d+", src, re.IGNORECASE)
    assert not re.search(r"\bround\s+\d+", src, re.IGNORECASE)
    assert not re.search(r"\battempt\s+\d+\b", src, re.IGNORECASE)
    assert not re.search(r"(?<![\w/.])[0-9a-f]{8}(?![\w/.])", src)


# ---------------------------------------------------------------------------
# Non-blocking / lifecycle behavior.
# ---------------------------------------------------------------------------


async def test_the_hook_returns_before_the_git_check_finishes():
    """`decide` gated on an `asyncio.Event`: the first call after a claim is
    latched returns `{}` immediately, without waiting on `decide` to
    resolve — proving the event loop stays free while the background check
    runs, not merely that the coroutine eventually finishes."""
    gate_event = asyncio.Event()
    started = asyncio.Event()

    async def decide(*, final_text):
        started.set()
        await gate_event.wait()
        return object()

    hook = LandedClaimGuardHook(decide=decide)
    hook.note_text(CLAIM_TEXT)

    result = await asyncio.wait_for(hook.hook({}, None, None), timeout=1)
    assert result == {}

    other_progressed = False

    async def other_work():
        nonlocal other_progressed
        await asyncio.sleep(0)
        other_progressed = True

    await asyncio.wait_for(started.wait(), timeout=1)
    await other_work()
    assert other_progressed, "the loop was blocked by the background check"

    gate_event.set()


async def test_the_hook_runs_no_git_subprocess_on_the_calling_thread(bare_repo, tmp_path, store, monkeypatch):
    """The blocking `_prefix()` closure inside `claim_gate_decision` (the
    `has_changes`/`commits_ahead` reads) must run off the event loop via
    `asyncio.to_thread`, not on the thread driving `hook()` calls. A
    report-kind task resolves entirely inside `_prefix()`, so every git
    call this scenario makes goes through that one dispatch."""
    calling_thread = threading.get_ident()
    seen_threads = []
    real_run = subprocess.run

    orch = _orch(store, tmp_path)
    task = _make_task(kind="investigation")
    stem = task.id[:8]
    _git(bare_repo, "checkout", "-b", stem)
    head = _git(bare_repo, "rev-parse", "HEAD").stdout.strip()
    repo = GitRepo(bare_repo)

    def _spy(cmd, *args, **kwargs):
        seen_threads.append(threading.get_ident())
        return real_run(cmd, *args, **kwargs)

    # Patched only around the drive itself: `subprocess` is one shared
    # module-level object, so patching it any earlier would also catch this
    # test's own `_git` setup helper, not just the guard's decision.
    monkeypatch.setattr(git_module.subprocess, "run", _spy)

    hook = _hook(orch, task, repo, base=stem, branch=stem)
    hook.note_text("already landed at " + head)
    injected = await _drive(hook)

    assert injected == ""
    assert seen_threads, "no subprocess.run observed — fixture did not reach the git layer"
    assert all(t != calling_thread for t in seen_threads)


async def test_fires_at_most_once_per_attempt(bare_repo, tmp_path, store):
    """Once the guard has rendered a result (even a silent `{}` on a
    resolved-but-non-refusing gate), it must never start a second
    background check or inject a second time."""
    orch = _orch(store, tmp_path)
    fired = _events(orch)
    task = _make_task()
    stem = task.id[:8]
    _git(bare_repo, "checkout", "-b", stem)
    (bare_repo / "work.txt").write_text("work\n")
    _git(bare_repo, "add", "-A")
    _git(bare_repo, "commit", "-m", "work")
    repo = GitRepo(bare_repo)

    hook = _hook(orch, task, repo, base=stem, branch=stem)
    hook.note_text(CLAIM_TEXT)
    first = await _drive(hook)
    assert "LANDED-CLAIM REFUSED" in first
    assert len(fired) == 1

    hook.note_text(CLAIM_TEXT)
    second = await hook.hook({}, None, None)
    assert second == {}
    assert len(fired) == 1


async def test_a_raising_decide_injects_nothing():
    """A `decide` that raises must be swallowed, not surfaced as a false
    refusal or propagated to crash the hook loop."""
    async def decide(*, final_text):
        raise RuntimeError("boom")

    fired = []
    hook = LandedClaimGuardHook(decide=decide, on_event=lambda k, t: fired.append((k, t)))
    hook.note_text(CLAIM_TEXT)
    injected = await _drive(hook)

    assert injected == ""
    assert fired == []


async def test_no_claim_text_never_calls_decide():
    """Prose that names no landed-work claim must never even start the
    background check."""
    called = False

    async def decide(*, final_text):
        nonlocal called
        called = True
        return object()

    hook = LandedClaimGuardHook(decide=decide)
    hook.note_text("Still working through the failing tests.")
    result = await hook.hook({}, None, None)

    assert result == {}
    assert called is False


# ---------------------------------------------------------------------------
# Wiring / ordering (AC1 / AC6 — the guard is really installed where the
# plan says, and nowhere it should not be).
# ---------------------------------------------------------------------------


def test_the_guard_is_ordered_second_after_receipts():
    r, lint, scope, types, g = object(), object(), object(), object(), object()
    assert Orchestrator._ordered_post_tool_hooks(
        r, lint, scope, types, landed_hook=g,
    ) == [r, g, lint, types, scope]
    assert Orchestrator._ordered_post_tool_hooks(
        r, lint, scope, types,
    ) == [r, lint, types, scope]


def test_run_attempt_constructs_the_guard_and_feeds_it_from_the_event_stream():
    """AST checks that `_run_attempt` builds a `LandedClaimGuardHook` and
    that the event stream feeds it via `note_text` — the two wiring facts
    a source-text `grep` could miss silently after a refactor, but an AST
    walk catches structurally."""
    import no_human.core.orchestrator as orchestrator_module
    src = Path(orchestrator_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def find(name):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        raise AssertionError(f"{name} not found")

    run_attempt = find("_run_attempt")
    run_attempt_src = ast.get_source_segment(src, run_attempt)
    assert "LandedClaimGuardHook(" in run_attempt_src

    assert "guard.note_text(event.text)" in src or "sv.note_text(event.text)" in src, (
        "no call feeding guard text from the event stream was found")
