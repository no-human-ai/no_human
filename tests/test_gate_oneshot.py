"""`nh gate` / `no_human.review.oneshot.run_gate` — the one-shot review gate.

Idiom: real temp git repos driven with `subprocess` (as in
`tests/test_approve_ready_cli.py`); the reviewer is always faked (no network,
no tokens) by monkeypatching `no_human.review.oneshot.AdversarialReviewer`
with a stub whose `review()` returns a canned `ReviewDecision` — this file
never talks to a real model. The credential check
(`no_human.review.oneshot.assert_subscription_mode`/`find_claude_cli`) is
monkeypatched to succeed by default and made to fail only in the tests that
exercise the refusal path.

`origin` here is a real local bare repo (a plain directory, `git init
--bare`) — pushing to it and reading from it is ordinary local filesystem
git, not network I/O, so `GitRepo.default_branch()` resolves
`refs/remotes/origin/HEAD` the same way it would against a real remote,
without this suite touching the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from no_human.cli.commands import gate
from no_human.review import oneshot
from no_human.review.oneshot import GateUnavailable, GateResult, render_markdown, run_gate
from no_human.review.reviewer import ReviewDecision
from no_human.review.selfcheck import ChecklistItem
from no_human.config import MissingCredentialError

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# git plumbing — real temp repos                                              #
# --------------------------------------------------------------------------- #

def _git(repo_path, *args):
    subprocess.run(["git", "-C", str(repo_path), *args], check=True,
                    capture_output=True)


def _git_out(repo_path, *args):
    return subprocess.run(["git", "-C", str(repo_path), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


def _make_repo_with_origin(tmp_path, name="repo"):
    """A `main` with one commit, pushed to a real local bare `origin`, with
    `refs/remotes/origin/HEAD` resolved — everything `default_branch()`
    needs, with no network."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                    check=True, capture_output=True)
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "-a")
    return repo, bare


def _add_test_file(repo, tests=3):
    body = "\n".join(
        f"def test_{i}():\n    assert {i} == {i}\n" for i in range(tests)
    )
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_x.py").write_text(body)
    _git(repo, "add", "tests/test_x.py")
    _git(repo, "commit", "-m", "add tests")


# --------------------------------------------------------------------------- #
# credential-check faking                                                     #
# --------------------------------------------------------------------------- #

class _FakeConfig:
    data: dict = {}

    def get(self, key, default=None):
        return self.data.get(key, default)


def _ok_credential(monkeypatch):
    monkeypatch.setattr(oneshot, "find_claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(oneshot, "load_config", lambda: _FakeConfig())
    monkeypatch.setattr(oneshot, "assert_subscription_mode", lambda **kw: None)


def _stub_reviewer(decision: ReviewDecision):
    class _Stub:
        @classmethod
        def from_config(cls, data, **kw):
            return cls()

        async def review(self, task, *, repo_path, diff_override, before_ref, **kw):
            return decision

    return _Stub


_PASSING_DECISION = ReviewDecision(
    passed=True,
    checklist=[ChecklistItem(label="no blocking findings", passed=True)],
)


# --------------------------------------------------------------------------- #
# 1. citations render                                                        #
# --------------------------------------------------------------------------- #

def test_the_gate_renders_file_and_line_citations(tmp_path, monkeypatch):
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")

    _ok_credential(monkeypatch)
    decision = ReviewDecision(
        passed=False,
        checklist=[
            ChecklistItem(label="unchecked return value", passed=False,
                          file="src/a.py", line=41, comment="ignored error"),
        ],
        demoted_citations=["stale claim: file moved"],
    )
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(decision))

    import asyncio
    result = asyncio.run(run_gate(repo))
    text = render_markdown(result)
    assert "src/a.py:41" in text
    assert "unchecked return value" in text
    assert "stale claim: file moved" in text
    assert result.passed is False


# --------------------------------------------------------------------------- #
# 2. exactly one one-shot construction site                                   #
# --------------------------------------------------------------------------- #

def test_only_one_module_constructs_the_oneshot_reviewer_call():
    """`diff_override=` marks the one-shot/no-tools review path. The
    orchestrator's existing PR-review call site and the reviewer's own
    definition are pre-existing; `oneshot.py` must be the only new one under
    `src/`."""
    allowed = {"orchestrator.py", "reviewer.py", "oneshot.py"}
    hits = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        if path.name in allowed:
            continue
        if "diff_override=" in path.read_text(encoding="utf-8"):
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert not hits, f"unexpected one-shot review call site(s): {hits}"


def test_the_gate_verb_delegates_to_run_gate(tmp_path, monkeypatch):
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")

    calls = []

    async def _fake_run_gate(repo_path, **kw):
        calls.append((repo_path, kw))
        return GateResult(
            passed=True, comparison="x", before_ref="a", after_ref="b",
            mode="branch", tamper=_clean_tamper(), decision=_PASSING_DECISION,
            uncommitted=[],
        )

    monkeypatch.setattr(oneshot, "run_gate", _fake_run_gate)
    result = CliRunner().invoke(gate, ["--repo", str(repo)])
    assert result.exit_code == 0
    assert calls, "the CLI verb must delegate to oneshot.run_gate"


# --------------------------------------------------------------------------- #
# 3. no Store, no server, no onboarding                                       #
# --------------------------------------------------------------------------- #

def test_the_gate_runs_with_no_store_and_no_server(tmp_path, monkeypatch):
    from no_human.core.db import Store

    def _boom(*a, **kw):
        raise AssertionError("run_gate must never construct a Store")

    monkeypatch.setattr(Store, "__init__", _boom)

    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    import asyncio
    result = asyncio.run(run_gate(repo))
    assert result.passed is True


# --------------------------------------------------------------------------- #
# 4. tamper finding on a weakened branch / pass on a clean branch             #
# --------------------------------------------------------------------------- #

def _clean_tamper():
    from no_human.testing.tamper_guard import TamperReport
    return TamperReport(tampered=False, tests_before=0, tests_after=0,
                        assertions_before=0, assertions_after=0)


def test_a_branch_that_deletes_a_test_reports_the_tamper_finding(tmp_path, monkeypatch):
    repo, _bare = _make_repo_with_origin(tmp_path)
    _add_test_file(repo, tests=3)
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "-b", "feature")
    _git(repo, "rm", "tests/test_x.py")
    _git(repo, "commit", "-m", "remove tests")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    import asyncio
    result = asyncio.run(run_gate(repo))
    text = render_markdown(result)
    assert result.tamper.tampered is True
    assert "TAMPERED" in text
    assert result.passed is False, "a tamper finding blocks the gate even if the reviewer passed"


def test_a_clean_branch_reports_a_pass(tmp_path, monkeypatch):
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "add a file, no tests touched")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    import asyncio
    result = asyncio.run(run_gate(repo))
    assert result.passed is True
    assert result.tamper.tampered is False


# --------------------------------------------------------------------------- #
# 5. never writes to the repo                                                #
# --------------------------------------------------------------------------- #

def _repo_fingerprint(repo):
    return (
        _git_out(repo, "rev-parse", "HEAD"),
        _git_out(repo, "status", "--porcelain"),
        _git_out(repo, "branch", "--list"),
    )


def test_the_gate_makes_no_repo_writes(tmp_path, monkeypatch):
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    before = _repo_fingerprint(repo)
    import asyncio
    asyncio.run(run_gate(repo))
    after = _repo_fingerprint(repo)
    assert before == after


def test_the_gate_never_shells_out_to_a_write_command(tmp_path, monkeypatch):
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    real_run = subprocess.run
    write_verbs = {"commit", "push", "merge", "checkout", "reset", "rebase"}

    def _spy(argv, *a, **kw):
        if argv and argv[0] == "git":
            bad = write_verbs & set(argv)
            assert not bad, f"gate ran a write command: {argv}"
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)
    import asyncio
    asyncio.run(run_gate(repo))


# --------------------------------------------------------------------------- #
# 6. credential refusal                                                       #
# --------------------------------------------------------------------------- #

def test_no_credential_refuses_and_names_what_is_missing(tmp_path, monkeypatch):
    repo, _bare = _make_repo_with_origin(tmp_path)
    monkeypatch.setattr(oneshot, "find_claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(oneshot, "load_config", lambda: _FakeConfig())

    def _raise(**kw):
        raise MissingCredentialError("no subscription token on file")

    monkeypatch.setattr(oneshot, "assert_subscription_mode", _raise)

    import asyncio
    with pytest.raises(GateUnavailable, match="no subscription token on file"):
        asyncio.run(run_gate(repo))


def test_the_verb_exits_2_and_prints_no_pass(tmp_path, monkeypatch):
    repo, _bare = _make_repo_with_origin(tmp_path)

    async def _raise(repo_path, **kw):
        raise GateUnavailable("no credential: no subscription token on file")

    monkeypatch.setattr(oneshot, "run_gate", _raise)
    result = CliRunner().invoke(gate, ["--repo", str(repo)])
    assert result.exit_code == 2
    assert "cannot run the gate" in result.output
    assert "PASS" not in result.output


# --------------------------------------------------------------------------- #
# 7. no upstream / undiffable tree / PR fetch failure                        #
# --------------------------------------------------------------------------- #

def test_no_upstream_refuses_by_name(tmp_path, monkeypatch):
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")

    _ok_credential(monkeypatch)
    import asyncio
    with pytest.raises(GateUnavailable, match="no upstream"):
        asyncio.run(run_gate(repo))


def test_not_a_git_repo_refuses_by_name(tmp_path, monkeypatch):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    _ok_credential(monkeypatch)
    import asyncio
    with pytest.raises(GateUnavailable, match="not a git repository"):
        asyncio.run(run_gate(not_a_repo))


def test_pr_fetch_failure_refuses_by_name(tmp_path, monkeypatch):
    repo, _bare = _make_repo_with_origin(tmp_path)
    _ok_credential(monkeypatch)
    import asyncio
    with pytest.raises(GateUnavailable, match="could not fetch pull request #7"):
        asyncio.run(run_gate(
            repo, pr_url="https://github.com/acme/widgets/pull/7",
        ))


# --------------------------------------------------------------------------- #
# 8. PR mode reviews the PR head's actual tree, not the user's checkout       #
# --------------------------------------------------------------------------- #

def _push_pr_ref(bare, from_repo_commit_path, number):
    """Build a PR branch in a throwaway clone of `bare` and push it to
    `refs/pull/<number>/head` — exactly the ref shape a real GitHub PR fetch
    resolves, and exactly what `_resolve_pr_mode`'s
    `git fetch origin refs/pull/<n>/head` reads."""
    pr_src = from_repo_commit_path
    subprocess.run(["git", "clone", "-q", str(bare), str(pr_src)],
                    check=True, capture_output=True)
    _git(pr_src, "config", "user.email", "t@example.com")
    _git(pr_src, "config", "user.name", "t")
    return pr_src


def test_pr_mode_reviews_the_pr_heads_tree_not_the_users_checkout(tmp_path, monkeypatch):
    """Regression for: PR mode used to hand the reviewer `repo_path` itself —
    the user's own checkout, still on `main` — so a citation naming a file
    that only exists at the PR head was checked against the wrong tree. The
    reviewer must instead see a tree that really is the PR head's content."""
    repo, bare = _make_repo_with_origin(tmp_path)

    pr_src = _push_pr_ref(bare, tmp_path / "pr_src", 9)
    (pr_src / "only_in_pr.py").write_text("x = 1\n")
    _git(pr_src, "add", "only_in_pr.py")
    _git(pr_src, "commit", "-m", "pr adds only_in_pr.py")
    _git(pr_src, "push", "origin", "HEAD:refs/pull/9/head")

    _ok_credential(monkeypatch)

    seen = {}

    class _Spy:
        @classmethod
        def from_config(cls, data, **kw):
            return cls()

        async def review(self, task, *, repo_path, diff_override, before_ref, **kw):
            seen["repo_path"] = Path(repo_path)
            seen["has_pr_file"] = (Path(repo_path) / "only_in_pr.py").exists()
            return _PASSING_DECISION

    monkeypatch.setattr(oneshot, "AdversarialReviewer", _Spy)

    import asyncio
    asyncio.run(run_gate(repo, pr_url="https://github.com/acme/widgets/pull/9"))

    assert seen["has_pr_file"] is True, (
        "the reviewer must see the PR head's real files, not the user's checkout"
    )
    assert seen["repo_path"] != repo, (
        "PR mode must not hand the reviewer the user's own checkout as repo_path"
    )
    assert not (repo / "only_in_pr.py").exists(), (
        "the user's own checkout must remain untouched by PR-mode review"
    )
    assert not seen["repo_path"].exists(), (
        "the throwaway PR-head clone must be removed once the gate finishes"
    )


def test_pr_mode_makes_no_writes_to_the_users_checkout(tmp_path, monkeypatch):
    repo, bare = _make_repo_with_origin(tmp_path)
    pr_src = _push_pr_ref(bare, tmp_path / "pr_src2", 11)
    (pr_src / "c.txt").write_text("pr change\n")
    _git(pr_src, "add", "c.txt")
    _git(pr_src, "commit", "-m", "pr change")
    _git(pr_src, "push", "origin", "HEAD:refs/pull/11/head")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    before = _repo_fingerprint(repo)
    import asyncio
    asyncio.run(run_gate(repo, pr_url="https://github.com/acme/widgets/pull/11"))
    after = _repo_fingerprint(repo)
    assert before == after


def test_pr_mode_never_shells_out_to_a_write_command_against_the_users_checkout(
    tmp_path, monkeypatch,
):
    repo, bare = _make_repo_with_origin(tmp_path)
    pr_src = _push_pr_ref(bare, tmp_path / "pr_src3", 13)
    (pr_src / "d.txt").write_text("pr change\n")
    _git(pr_src, "add", "d.txt")
    _git(pr_src, "commit", "-m", "pr change")
    _git(pr_src, "push", "origin", "HEAD:refs/pull/13/head")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    real_run = subprocess.run
    write_verbs = {"commit", "push", "merge", "checkout", "reset", "rebase"}
    real_repo = str(repo.resolve())

    def _spy(argv, *a, **kw):
        if argv and argv[0] == "git":
            cwd = kw.get("cwd")
            targets_real_repo = (
                (cwd is not None and str(Path(cwd).resolve()) == real_repo)
                or real_repo in argv
            )
            if targets_real_repo:
                bad = write_verbs & set(argv)
                assert not bad, (
                    f"gate ran a write command against the user's own "
                    f"checkout: {argv}"
                )
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)
    import asyncio
    asyncio.run(run_gate(repo, pr_url="https://github.com/acme/widgets/pull/13"))
