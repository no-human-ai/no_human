"""Constraint #2 ("the agent commits under a distinct identity") is EXPORTED
by `Orchestrator._agent_git_identity` but was not ENFORCED: a live rig showed
that `git commit --author=`, `env -u GIT_AUTHOR_NAME -u GIT_AUTHOR_EMAIL
-u GIT_COMMITTER_NAME -u GIT_COMMITTER_EMAIL git commit`, and `git -c
user.email=` all land a commit stamped with the OPERATOR's identity instead
of the agent's, and none of them are denied by `agent/guard.py` (a
PreToolUse hook that only exists on the Claude backend to begin with).

DECISION RECORDED HERE (option 1, post-hoc, not option 2, not both — the
reasoning also lives next to `Orchestrator._foreign_authored_commits` and
`GitRepo.commit_identities`): a PreToolUse denial list is backend-specific
(`agent/codex_backend.py` has no veto seam at all, see test 6) and the set
of spellings that can re-stamp a commit is not closed. Reading back what git
itself recorded for every commit in the attempt's window
(`merge-base(base, HEAD)..HEAD`, the same window the tamper guard, the
reviewer and the PR diff already use) catches all of them, including ones
not yet invented, in one place, on the one path both backends share
(`Orchestrator._run_attempt`).

Three cases this file holds green together (the ordinary self-check a
detector like this needs): a genuine mismatch is caught (1, 2, 5, 6), an
honestly-authored commit is left alone (3), and the pipeline's OWN commits
are immune to an operator identity merely being exported into the process
env (4, via `GitRepo`'s `_IDENTITY_ENV` scrub). This is a DETECTOR, not a
lock: it fails the attempt after the fact, and test 7 pins that no comment,
docstring or doc dresses that up as "forgery is impossible".

FAIL-CLOSED ON A BROKEN READ (tests 4a/4b below): a review of an earlier
version of this fix found that `commit_identities` used `check=False`, so a
`git log` failure (bad base ref, corrupt history) returned an EMPTY list —
the identical value a genuinely clean branch produces — and the orchestrator
caught the resulting (never-actually-raised) exception by ALSO returning
`[]`. Both layers were pass-shaped on failure: a broken gate silently read
as a passing one. `commit_identities` now raises `GitError` on a read
failure (a genuine empty range still returns `[]`, since `git log` exits 0
for that), and `_foreign_authored_commits` turns that into a diagnostic
offender the caller fails the attempt on — loud and stopped, not quiet and
clean.
"""

from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest

from no_human.agent.claude_backend import AgentResult
from no_human.config import load_config
from no_human.core.orchestrator import Orchestrator
from no_human.core.task import Task, TaskStatus
from no_human.notify.slack import SlackNotifier
from no_human.vcs.git import CommitIdentity, GitError, GitRepo

# The identity `_agent_git_identity()` defaults to (`config.py` / no `git:`
# section set) — matches every fixture below that doesn't override it.
_AGENT_NAME = "no_human"
_AGENT_EMAIL = "no-human@acme.com"

# An "operator" identity distinct from the agent's, used as the repo's
# `git config user.*` in every fixture — the identity a stripped-env commit
# falls back to.
_OPERATOR_NAME = "Eyal Golan"
_OPERATOR_EMAIL = "eyal@example.com"

_AGENT_ENV = {
    "GIT_AUTHOR_NAME": _AGENT_NAME, "GIT_AUTHOR_EMAIL": _AGENT_EMAIL,
    "GIT_COMMITTER_NAME": _AGENT_NAME, "GIT_COMMITTER_EMAIL": _AGENT_EMAIL,
}


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo_on_main(tmp_path, name="work"):
    """A repo on branch `main`, `git config user.*` set to the OPERATOR
    identity — the identity any commit that loses the agent's env vars
    falls back to."""
    work = tmp_path / name
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", _OPERATOR_EMAIL)
    _git(work, "config", "user.name", _OPERATOR_NAME)
    (work / "f.txt").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "checkout", "-q", "-b", "nh/attempt-1")
    return work


def _orch(tmp_path):
    """A lightweight Orchestrator for calling `_foreign_authored_commits`/
    `_agent_git_identity` directly — no DB, no real backend needed, mirrors
    `tests/test_run_84251cb2_regressions.py::_orch_for_guards`."""

    class _Backend:
        model = "claude-sonnet-5"
        never_push_to = ["main"]

    cfg = load_config(tmp_path / "config.yaml")
    return Orchestrator(None, cfg.data, _Backend(), SlackNotifier(None))


# --------------------------------------------------------------------------- #
# (1)/(2) — detection, several spellings                                      #
# --------------------------------------------------------------------------- #


def test_env_u_git_author_commit_is_detected(tmp_path):
    """AC #2 (part 1). A commit made via the exact live-rig spelling —
    `env -u GIT_AUTHOR_NAME -u GIT_AUTHOR_EMAIL -u GIT_COMMITTER_NAME
    -u GIT_COMMITTER_EMAIL git commit` — while the agent's identity vars ARE
    exported in the process env (as `_agent_git_identity()` exports them into
    the coder's env), still lands attributed to the operator's `git config`,
    and `_foreign_authored_commits` must name it.

    RED on base: `_foreign_authored_commits` and `GitRepo.commit_identities`
    do not exist there — this call raises `AttributeError`. Proven by
    `.no_human/repro_tests.json` (base run / fixed run), not by stashing the
    src change in-process here.
    """
    work = _repo_on_main(tmp_path)
    (work / "f.txt").write_text("agent change\n")
    env = {**os.environ, **_AGENT_ENV}
    subprocess.run(
        ["env", "-u", "GIT_AUTHOR_NAME", "-u", "GIT_AUTHOR_EMAIL",
         "-u", "GIT_COMMITTER_NAME", "-u", "GIT_COMMITTER_EMAIL",
         "git", "commit", "-am", "sneaky change"],
        cwd=work, env=env, check=True, capture_output=True, text=True,
    )
    sha = _git(work, "rev-parse", "HEAD")

    repo = GitRepo(work)
    orch = _orch(tmp_path)
    offenders = orch._foreign_authored_commits(repo, "main")

    assert len(offenders) == 1, offenders
    assert offenders[0].startswith(sha[:8])
    assert _OPERATOR_NAME in offenders[0] and _OPERATOR_EMAIL in offenders[0]
    assert "author=" in offenders[0] and "committer=" in offenders[0]


def test_author_flag_and_dash_c_spellings_are_detected_too(tmp_path):
    """AC #2 (part 2). Two more spellings than the live-rig one, neither
    enumerated anywhere in the fix — demonstrating the "read the result, not
    the command" property `_foreign_authored_commits`'s docstring claims.

    `--author=` overrides only the author; the committer stays the agent's
    (env vars are still exported for this one) — the commit is still an
    offender because author != agent."""
    work = _repo_on_main(tmp_path)
    (work / "f.txt").write_text("author flag change\n")
    env = {**os.environ, **_AGENT_ENV}
    subprocess.run(
        ["git", "commit", "-am", "author flag",
         f"--author={_OPERATOR_NAME} <{_OPERATOR_EMAIL}>"],
        cwd=work, env=env, check=True, capture_output=True, text=True,
    )
    sha_author_flag = _git(work, "rev-parse", "HEAD")

    # `-c user.name=`/`-c user.email=` only win when the GIT_* env vars are
    # NOT set (env outranks `-c`) — so unset them for this one commit, the
    # `env -u` spelling again but landing via `-c` instead of git config.
    (work / "f.txt").write_text("dash c change\n")
    subprocess.run(
        ["env", "-u", "GIT_AUTHOR_NAME", "-u", "GIT_AUTHOR_EMAIL",
         "-u", "GIT_COMMITTER_NAME", "-u", "GIT_COMMITTER_EMAIL",
         "git", "-c", f"user.name={_OPERATOR_NAME}",
         "-c", f"user.email={_OPERATOR_EMAIL}",
         "commit", "-am", "dash c"],
        cwd=work, env=env, check=True, capture_output=True, text=True,
    )
    sha_dash_c = _git(work, "rev-parse", "HEAD")

    repo = GitRepo(work)
    orch = _orch(tmp_path)
    offenders = orch._foreign_authored_commits(repo, "main")

    shas = {o[:8] for o in offenders}
    assert sha_author_flag[:8] in shas, offenders
    assert sha_dash_c[:8] in shas, offenders
    assert len(offenders) == 2, offenders


# --------------------------------------------------------------------------- #
# (3)/(4) — legitimate commits are unaffected                                 #
# --------------------------------------------------------------------------- #


def test_legitimate_agent_commits_pass(tmp_path):
    """AC #3. A commit made honestly under the agent's identity — whether
    because the process env carries it (as the coder's own `git commit`
    would inherit) or because `GitRepo.commit_all` stamped it with `-c
    user.name=`/`-c user.email=` — is not flagged."""
    work = _repo_on_main(tmp_path)

    (work / "f.txt").write_text("honest env commit\n")
    env = {**os.environ, **_AGENT_ENV}
    subprocess.run(["git", "commit", "-am", "honest"], cwd=work, env=env,
                   check=True, capture_output=True, text=True)

    repo = GitRepo(work, identity_name=_AGENT_NAME, identity_email=_AGENT_EMAIL)
    (work / "f.txt").write_text("honest commit_all commit\n")
    repo.commit_all("via commit_all")

    orch = _orch(tmp_path)
    offenders = orch._foreign_authored_commits(repo, "main")
    assert offenders == []


def test_pipeline_commits_ignore_exported_operator_identity(monkeypatch, tmp_path):
    """The `git.py` env-scrub (`_IDENTITY_ENV`, in `_run`): if the OPERATOR's
    shell happens to export `GIT_AUTHOR_*`/`GIT_COMMITTER_*` (env outranks
    `-c user.name=`), `GitRepo.commit_all` must still stamp its own commit
    with the configured agent identity, or this very gate would false-fire
    on the pipeline's own checkpoint commits."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", _OPERATOR_NAME)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", _OPERATOR_EMAIL)
    monkeypatch.setenv("GIT_COMMITTER_NAME", _OPERATOR_NAME)
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", _OPERATOR_EMAIL)

    work = _repo_on_main(tmp_path)
    repo = GitRepo(work, identity_name=_AGENT_NAME, identity_email=_AGENT_EMAIL)
    (work / "f.txt").write_text("pipeline commit under exported operator env\n")
    repo.commit_all("pipeline commit")

    ae, ce = _git(work, "log", "-1", "--format=%ae%x00%ce").split("\x00")
    assert ae == _AGENT_EMAIL, "author email leaked the exported operator env"
    assert ce == _AGENT_EMAIL, "committer email leaked the exported operator env"

    orch = _orch(tmp_path)
    offenders = orch._foreign_authored_commits(repo, "main")
    assert offenders == [], "the pipeline's own commit must not trip its own gate"


# --------------------------------------------------------------------------- #
# (4a)/(4b) — a broken read fails CLOSED, not open                            #
# --------------------------------------------------------------------------- #


def test_commit_identities_raises_on_unreadable_range(tmp_path):
    """`GitRepo.commit_identities` must not swallow a `git log` failure into
    an empty list — that's the exact same value a genuinely clean range
    produces. An unresolvable base ref raises `GitError` instead.

    RED on base: base's `commit_identities` calls `_run(..., check=False)`,
    so this raises nothing and returns `[]` — this assertion fails there."""
    work = _repo_on_main(tmp_path)
    repo = GitRepo(work)
    with pytest.raises(GitError):
        repo.commit_identities("totally-bogus-ref-does-not-exist")


def test_unreadable_history_fails_closed_not_silently_passes(monkeypatch, tmp_path):
    """AC #1 (the reviewer finding on attempt 1). `_foreign_authored_commits`
    must not turn a `commit_identities` read failure into `[]` — the caller
    treats `[]` as "no offenders, proceed", so that would make a broken gate
    a silently passing one. An unreadable window must return a NON-EMPTY
    diagnostic so the caller fails the attempt exactly as it would for a
    real mismatch.

    `commit_identities` is monkeypatched to raise directly (rather than
    driving a real `git log` failure through `_review_base`, whose own
    `except Exception: return "HEAD~1"` fallback would otherwise mask a bad
    `base` argument behind a coincidentally-valid window) — this isolates
    exactly the read-failure branch inside `_foreign_authored_commits`.

    RED on base: base's `except Exception: return []` returns `[]` here, so
    `offenders == []` would hold on base — the `offenders != []` assertion
    below fails there."""
    work = _repo_on_main(tmp_path)
    repo = GitRepo(work)

    def _boom(base):
        raise GitError(f"git log {base}..HEAD failed (128): fatal: bad revision")

    monkeypatch.setattr(GitRepo, "commit_identities", lambda self, base: _boom(base))

    orch = _orch(tmp_path)
    offenders = orch._foreign_authored_commits(repo, "main")

    assert offenders != [], (
        "a broken read must not be indistinguishable from a clean branch"
    )
    assert len(offenders) == 1
    assert "unverifiable" in offenders[0] or "could not read" in offenders[0]


def test_an_author_name_with_a_unicode_line_separator_is_not_silently_dropped(tmp_path):
    """Reviewer finding: `commit_identities` originally split `git log`
    output with `out.splitlines()`, which breaks on more than "\n" -- also
    \x0b, \x0c, \x1c-\x1e, \x85, U+2028 (LINE SEPARATOR) and U+2029
    (PARAGRAPH SEPARATOR). Measured directly (see this test's construction):
    when one of those characters sits in a field BEFORE the last one (e.g.
    the author name, settable via `git commit --author=`), splitlines()
    fractures the record at that character, and each resulting fragment
    carries only PART of the NUL-separated fields -- neither has exactly 6,
    so `if len(parts) != 6: continue` (the old code) silently dropped BOTH
    fragments. Since the sha itself lives in the first fragment, the ENTIRE
    commit -- sha, author, committer -- vanished from `commit_identities`'s
    return value, and `_foreign_authored_commits` (which only ever sees that
    return value) reported a clean branch for one that in fact has a
    foreign-authored commit on it. This is a stronger version of the same
    class of bug than putting the character in the subject (the LAST field):
    there, the fields before the break still parse as a valid 6-tuple and
    the record survives, just with a truncated subject -- the commit is NOT
    lost. It is a non-last field, like the author name here, that makes the
    whole record disappear.

    RED on base: base's `commit_identities` uses `out.splitlines()`, which
    fractures this record on the embedded separator and drops it entirely
    (verified interactively while building this fix: `out.splitlines()`
    parsing loses the sha, `out.split("\n")` parsing keeps it)."""
    work = _repo_on_main(tmp_path)
    weird_name = "Weird" + "\u2028" + "Name"
    env = dict(os.environ)
    for k in _AGENT_ENV:
        env.pop(k, None)
    (work / "f.txt").write_text("changed\n")
    subprocess.run(
        ["git", "-C", str(work), "commit", "-a", "-q", "-m", "msg",
         "--author", f"{weird_name} <weird@example.com>"],
        check=True, capture_output=True, text=True, env=env,
    )
    sha = _git(work, "rev-parse", "HEAD")

    repo = GitRepo(work)
    identities = repo.commit_identities("main")

    assert sha in {c.sha for c in identities}, (
        "the foreign-authored commit must not vanish from commit_identities "
        "just because its author name contains a Unicode line separator"
    )

    orch = _orch(tmp_path)
    offenders = orch._foreign_authored_commits(repo, "main")
    assert any(sha[:8] in o for o in offenders), (
        "a commit that survives commit_identities but is foreign-authored "
        "must still be flagged as an offender"
    )


# --------------------------------------------------------------------------- #
# (5)/(6) — the gate wired into `_run_attempt`, both backends                 #
# --------------------------------------------------------------------------- #


def _config(tmp_path):
    cfg = load_config(tmp_path / "config.yaml")
    cfg.data.setdefault("planning", {})["enabled"] = False
    cfg.data.setdefault("reviewer", {})["allow_advisory"] = True
    return cfg


@pytest.fixture
def bare_repo(tmp_path):
    """A local repo with a remote, on `main`, `git config user.*` set to the
    OPERATOR identity — mirrors `tests/test_infra_not_work.py::bare_repo`."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", _OPERATOR_EMAIL)
    _git(work, "config", "user.name", _OPERATOR_NAME)
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work


class _CommittingScriptedBackend:
    """Like `_ScriptedBackend` in `tests/test_infra_not_work.py`, but its one
    turn ALSO makes a commit with the live-rig `env -u GIT_AUTHOR_*` spelling
    — the exact shape of a coder's Bash tool re-attributing its own commit to
    the operator — before returning a scripted, non-error `AgentResult`. This
    exercises the real `_run_attempt` control flow: since the backend already
    committed everything (`repo.has_changes()` is False afterwards), the
    attempt takes the `resumed_commit` path straight to the post-commit gates
    without ever reaching the reviewer or push code, so this gate is the
    first thing that can catch it.
    """

    def __init__(self, result: AgentResult):
        self._result = result
        self.calls = 0

    async def run(self, prompt, *, cwd, max_turns, effort=None, resume=None,
                  on_event=None, supervisor_hook=None, **kwargs):
        self.calls += 1
        (Path(cwd) / "calc.py").write_text(
            "def add(a, b):\n    return a + b  # agent edit\n"
        )
        env = {**os.environ, **_AGENT_ENV}
        subprocess.run(
            ["env", "-u", "GIT_AUTHOR_NAME", "-u", "GIT_AUTHOR_EMAIL",
             "-u", "GIT_COMMITTER_NAME", "-u", "GIT_COMMITTER_EMAIL",
             "git", "commit", "-am", "sneaky change"],
            cwd=cwd, env=env, check=True, capture_output=True, text=True,
        )
        return self._result


def _turn_result(**overrides) -> AgentResult:
    fields = dict(
        final_text="done", num_turns=1, is_error=False, tokens_used=100,
        session_id="s", stop_reason="end_turn", cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    fields.update(overrides)
    return AgentResult(**fields)


async def _run_one_attempt(store, bare_repo, tmp_path, backend):
    cfg = _config(tmp_path)
    orch = Orchestrator(store, cfg.data, backend, SlackNotifier(None),
                        event_sink=[].append)
    task = Task.new("do a thing", repo_path=str(bare_repo))
    await store.create_task(task)
    await store.set_status(task, TaskStatus.CONTEXT)
    await store.set_status(task, TaskStatus.PLANNING)
    repo = GitRepo(bare_repo)
    return orch, task, repo


async def test_the_gate_fails_the_attempt_and_never_reaches_review(
        store, bare_repo, tmp_path):
    """AC #1. A fake backend commits under a mismatched identity, then
    reports a normal (non-error) turn — the reviewer/push machinery is never
    mocked because the gate must stop the attempt before either runs."""
    backend = _CommittingScriptedBackend(_turn_result())
    orch, task, repo = await _run_one_attempt(store, bare_repo, tmp_path, backend)
    events: list[dict] = []
    orch._sink = events.append

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls == 1, "the backend never ran — the test proves nothing"
    assert outcome.status == TaskStatus.FAILED
    assert "attribution" in outcome.detail or "identity" in outcome.detail

    mismatch_events = [e for e in events if e.get("kind") == "identity_mismatch"]
    assert mismatch_events, "no identity_mismatch event was emitted"

    attempts = await store.list_attempts(task.id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"


async def test_the_gate_runs_on_the_codex_backend_path(store, bare_repo, tmp_path):
    """AC #4. Same rig, but with a Codex-*shaped* stub backend, to prove the
    gate is on the path both backends share — `_run_attempt` never branches
    on backend type to decide whether to check attribution. Plus two source
    pins: the gate call exists and runs before any push/PR code, and
    `agent/codex_backend.py` really has no PreToolUse veto seam, which is
    the whole reason option 1 (post-hoc) rather than option 2 (a guard denial
    list) was chosen."""

    class _CodexLikeBackend(_CommittingScriptedBackend):
        """No `on_event`/PreToolUse-shaped machinery beyond what the base
        class already uses — stands in for `CodexBackend.run`, which also
        has no veto seam (see the source-pin assertion below)."""

    backend = _CodexLikeBackend(_turn_result())
    orch, task, repo = await _run_one_attempt(store, bare_repo, tmp_path, backend)

    outcome = await orch._run_attempt(task, repo, 1, "main")

    assert backend.calls == 1
    assert outcome.status == TaskStatus.FAILED

    src = inspect.getsource(Orchestrator._run_attempt)
    assert "self._foreign_authored_commits(" in src, (
        "the gate must live on `_run_attempt`, the one path both backends share"
    )
    # `repo.push(branch)` (:6044) is the one real push call `_run_attempt`
    # makes on the success path — a bare "open_pr" substring is NOT a safe
    # marker, since an early comment (:4205, "which is where `open_pr`
    # lives") references it long before the gate does.
    idx_push = src.index("repo.push(branch)")
    assert src.index("self._foreign_authored_commits(") < idx_push, (
        "the gate must run before anything is pushed"
    )

    codex_src = inspect.getsource(
        __import__("no_human.agent.codex_backend", fromlist=["codex_backend"])
    )
    assert "No PreToolUse veto" in codex_src or "no PreToolUse" in codex_src.lower(), (
        "the whole reason this check must be post-hoc is that codex_backend "
        "has no pre-execution veto seam — pin that fact so it can't drift"
    )


# --------------------------------------------------------------------------- #
# (7) — no doc claims forgery is impossible                                   #
# --------------------------------------------------------------------------- #

_BAD_FORGERY_CLAIMS = (
    "forgery is impossible", "prevents forgery", "cannot be forged",
    "forgery-proof", "unforgeable", "makes forgery impossible",
    "impossible to forge", "identity impossible to fake",
)


def _assert_only_honest_disclaimer(text: str, honest_phrase: str) -> None:
    """`text` is allowed exactly one appearance of the word "impossible": the
    honest admission that this check does NOT make forgery impossible.
    Strip that one phrase out and require zero leftover "impossible"/
    forgery-is-solved claims in what remains — a naive `"impossible" not in
    text` would false-positive on the honest disclaimer itself.

    Both source docstrings and the doc wrap this sentence across lines, so
    the literal string has embedded newlines/indentation where the phrase
    below has single spaces — collapse all whitespace runs to one space
    before comparing, on both sides."""
    import re

    lowered = re.sub(r"\s+", " ", text.lower())
    honest_norm = re.sub(r"\s+", " ", honest_phrase.lower())
    assert honest_norm in lowered, (
        f"expected the honest non-claim {honest_phrase!r} to be present"
    )
    remainder = lowered.replace(honest_norm, "", 1)
    assert "impossible" not in remainder, (
        "found an extra 'impossible' beyond the one honest disclaimer — "
        "check it isn't dressed up as prevention"
    )
    for bad in _BAD_FORGERY_CLAIMS:
        assert bad not in remainder, f"found a forgery-is-solved claim: {bad!r}"


def test_no_doc_or_docstring_claims_forgery_is_impossible():
    """AC #5. The check DETECTS and FAILS the attempt; it must never be
    written up — in code or docs — as making forgery impossible."""
    orch_doc = inspect.getsource(Orchestrator._foreign_authored_commits)
    _assert_only_honest_disclaimer(
        orch_doc, "does not and cannot make forgery impossible"
    )

    git_doc = inspect.getsource(GitRepo.commit_identities)
    lowered = git_doc.lower()
    assert "impossible" not in lowered
    for bad in _BAD_FORGERY_CLAIMS:
        assert bad not in lowered

    security_md = Path(__file__).resolve().parents[1] / "docs" / "security.md"
    text = security_md.read_text()
    # Scope to §3 ("Deterministic VCS under a distinct identity") only — an
    # unrelated later section legitimately discusses a different feature's
    # limits in similar language and would otherwise be a false positive.
    section = text.split("## 3.")[1].split("## 4.")[0]
    _assert_only_honest_disclaimer(
        section, "does not and cannot make forging the identity impossible"
    )
