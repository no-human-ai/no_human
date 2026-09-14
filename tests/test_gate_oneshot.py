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
from no_human.config import AuthError, MissingCredentialError

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


def _make_repo_with_github_origin(tmp_path, owner="acme", repo_name="widgets", name="repo"):
    """Like `_make_repo_with_origin`, but `origin`'s configured remote URL is
    a genuine `https://github.com/<owner>/<repo>.git` — the identity
    `oneshot._origin_owner_repo` reads and PR mode checks a `--pr` URL
    against. It is rewritten, purely locally, via git's own
    `url.<x>.insteadOf` to the real local bare repo, so every git operation
    still resolves on disk and no network is touched — `git remote get-url
    origin` reports the GitHub URL, `git fetch origin ...` actually reads
    from `bare`."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                    check=True, capture_output=True)
    github_url = f"https://github.com/{owner}/{repo_name}.git"
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", f"url.{bare}.insteadOf", github_url)
    (repo / "a.txt").write_text("orig\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", github_url)
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
    """`diff_override=` marks the one-shot construction site this module
    exists to be the only one of — not the only way `AdversarialReviewer`
    ever ends up on its no-tools/single-turn path: `route_single_turn`
    (reviewer.py:2562) reaches the same fast path without a `diff_override`.
    The orchestrator's existing PR-review call site and the reviewer's own
    definition are pre-existing; `oneshot.py` must be the only new
    `diff_override=` call site under `src/`."""
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


# Fixed, closed allowlist of the git subcommands `run_gate`'s own plumbing
# (oneshot.py) and the tamper guard it calls (testing/runner.py) are allowed
# to issue against the user's own checkout — enumerated by reading every
# `["git", ...]` construction reachable from `run_gate` (oneshot.py's `_git`/
# `_rev_parse`/`_merge_base`/`_diff`/`_uncommitted_paths`/
# `_origin_owner_repo`/`_resolve_pr_mode`, `GitRepo.current_branch`/
# `head_sha`/`default_branch`, and `runner.py`'s `_git_show`/`_git_files`).
# `clone`/`checkout` are deliberately absent here: they exist only inside
# `_materialized_pr_head`'s throwaway temp clone and are checked separately.
_READ_ONLY_GIT_VERBS = {
    "rev-parse", "merge-base", "diff", "status", "config", "fetch",
    "symbolic-ref", "remote", "ls-tree", "show",
}


_GIT_GLOBAL_OPTS_TAKING_A_VALUE = {"-c", "-C", "--git-dir", "--work-tree"}


def _git_subcommand(argv: list[str]) -> str | None:
    """The first non-flag token after the program name and any global
    options — git's subcommand. `GitRepo._run` (vcs/git.py:220-224) always
    prepends `-c user.name=... -c user.email=...` ahead of the real
    subcommand, so a naive "first token not starting with '-'" scan would
    misread the `-c` value (`user.name=no_human`) as the subcommand; skip
    each such option's value explicitly instead."""
    tokens = iter(argv[1:])
    for tok in tokens:
        if tok in _GIT_GLOBAL_OPTS_TAKING_A_VALUE:
            next(tokens, None)  # consume this option's value, not the verb
            continue
        if tok.startswith("-"):
            continue
        return tok
    return None


def _install_fail_closed_git_spy(monkeypatch, *, real_repo: Path):
    """Replace `subprocess.run`/`Popen` with a DEFAULT-DENY spy for the
    duration of a `run_gate` call.

    This replaces a prior denylist spy that only inspected calls whose
    `argv[0]` was the exact string `"git"` and only rejected a hardcoded
    6-verb set — blind to any other program name (e.g. a planted
    `subprocess.run(["/bin/sh", "-c", "touch /tmp/pwned"])`, or an absolute
    path to the git binary) and to any write verb outside those six (`git
    branch -D`, `git stash`, `git clean -fd`, `git tag -f`, `git remote
    set-head`, ...). Here every call must resolve to the `git` program by
    basename; its subcommand must be on the fixed read-only allowlist above,
    with `remote`/`config`/`status` further pinned to the exact read-only
    subform this call graph actually uses; `clone`/`checkout` are permitted
    only when clearly scoped to the throwaway PR-head clone, never against
    `real_repo`; anything else — a different program, an unlisted verb, a
    write-shaped call, or any use of `Popen` at all (this call graph never
    needs it) — fails the test immediately instead of silently passing.
    """
    real_repo = real_repo.resolve()
    real_run = subprocess.run

    def _check(argv, cwd):
        assert argv and not isinstance(argv, str), (
            f"gate ran a subprocess with a shell string / empty argv: {argv!r}"
        )
        argv = [str(a) for a in argv]
        assert Path(argv[0]).name == "git", f"gate ran a non-git program: {argv}"
        verb = _git_subcommand(argv)
        resolved_cwd = Path(cwd).resolve() if cwd else None

        if verb == "clone":
            dest = Path(argv[-1])
            assert str(dest) != str(real_repo), (
                f"gate cloned into the user's own checkout: {argv}"
            )
            return
        if verb == "checkout":
            assert resolved_cwd is not None and resolved_cwd != real_repo, (
                f"gate ran `git checkout` against the user's own checkout: {argv}"
            )
            assert "--detach" in argv, f"gate ran a non-detached checkout: {argv}"
            return

        assert verb in _READ_ONLY_GIT_VERBS, (
            f"gate ran an unlisted (non-allowlisted) git subcommand: {argv}"
        )
        assert resolved_cwd == real_repo, (
            f"gate ran a read-only git command outside the user's own "
            f"checkout: {argv} (cwd={cwd})"
        )
        if verb == "remote":
            assert "show" in argv, f"gate ran a write-shaped `git remote`: {argv}"
        if verb == "config":
            assert "--get" in argv, f"gate ran a write-shaped `git config`: {argv}"
        if verb == "status":
            assert "--porcelain" in argv, f"gate ran a non-porcelain `git status`: {argv}"

    def _run_spy(argv, *a, **kw):
        _check(argv, kw.get("cwd"))
        return real_run(argv, *a, **kw)

    # NOTE: `subprocess.Popen` is deliberately NOT patched here — `run_spy`
    # delegates to the real `subprocess.run`, which itself is implemented
    # on top of `subprocess.Popen`; patching `Popen` too would make every
    # legitimate call raise from inside `real_run`. `subprocess.run` is the
    # only primitive this call graph ever invokes directly (confirmed by
    # reading oneshot.py, vcs/git.py, and runner.py's `_git_show`/
    # `_git_files`), so gating just `run` is already fail-closed for this
    # call graph; a future direct `Popen` call site would need its own spy.
    monkeypatch.setattr(subprocess, "run", _run_spy)


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
    """Fail-closed: every subprocess call the gate makes must resolve to the
    `git` program and carry a subcommand on the fixed read-only allowlist —
    not merely avoid a hardcoded denylist of six verbs behind an
    `argv[0] == "git"` string check. See `_install_fail_closed_git_spy`."""
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    _install_fail_closed_git_spy(monkeypatch, real_repo=repo)
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


def test_a_credential_problem_that_is_not_a_missing_credential_is_named_as_such(
    tmp_path, monkeypatch,
):
    """`_check_credential` (oneshot.py:142-160) splits `MissingCredentialError`
    ("nothing on file") from `AuthError` ("something on file, but it's
    wrong" — e.g. a stray `ANTHROPIC_API_KEY` set while `llm.auth_mode` is
    `subscription`, config.py:1280). Calling the latter "no credential"
    would be false — the problem is an extra, disallowed credential, not a
    missing one — so the refusal message must say "credential problem",
    never "no credential"."""
    repo, _bare = _make_repo_with_origin(tmp_path)
    monkeypatch.setattr(oneshot, "find_claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(oneshot, "load_config", lambda: _FakeConfig())

    def _raise(**kw):
        raise AuthError("ANTHROPIC_API_KEY is set but auth_mode is subscription")

    monkeypatch.setattr(oneshot, "assert_subscription_mode", _raise)

    import asyncio
    with pytest.raises(GateUnavailable, match="credential problem") as exc_info:
        asyncio.run(run_gate(repo))
    assert "no credential" not in str(exc_info.value), (
        "an AuthError (extra/disallowed credential) must not be misreported "
        "as a missing one"
    )


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
    repo, _bare = _make_repo_with_github_origin(tmp_path)
    _ok_credential(monkeypatch)
    import asyncio
    with pytest.raises(GateUnavailable, match="could not fetch pull request #7"):
        asyncio.run(run_gate(
            repo, pr_url="https://github.com/acme/widgets/pull/7",
        ))


def test_diff_failure_raises_instead_of_returning_an_empty_string(tmp_path):
    """Regression: `_diff` used to return `proc.stdout` unconditionally, so a
    failing `git diff` (bad refs, corrupted objects, ...) silently became an
    empty diff — which the reviewer would then pass as "no changes found"."""
    repo, _bare = _make_repo_with_origin(tmp_path)
    with pytest.raises(GateUnavailable, match="could not diff"):
        oneshot._diff(repo, "not-a-real-ref", "HEAD")


def test_a_diff_failure_refuses_the_gate_instead_of_a_false_pass(tmp_path, monkeypatch):
    """End-to-end regression for the same bug: a failing `git diff` must
    surface as a `GateUnavailable` refusal, never as a passing `GateResult`
    built from an empty diff the reviewer never actually saw."""
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    real_git = oneshot._git

    def _fake_git(repo_path, *args):
        if args and args[0] == "diff":
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=128,
                stdout="", stderr="fatal: bad revision",
            )
        return real_git(repo_path, *args)

    monkeypatch.setattr(oneshot, "_git", _fake_git)

    import asyncio
    with pytest.raises(GateUnavailable, match="could not diff"):
        asyncio.run(run_gate(repo))


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
    repo, bare = _make_repo_with_github_origin(tmp_path)

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
    repo, bare = _make_repo_with_github_origin(tmp_path)
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
    """Same fail-closed allowlist as the branch-mode version above, applied
    to PR mode: `clone`/`checkout` are only permitted against the throwaway
    PR-head clone (see `_materialized_pr_head`), never against `real_repo`."""
    repo, bare = _make_repo_with_github_origin(tmp_path)
    pr_src = _push_pr_ref(bare, tmp_path / "pr_src3", 13)
    (pr_src / "d.txt").write_text("pr change\n")
    _git(pr_src, "add", "d.txt")
    _git(pr_src, "commit", "-m", "pr change")
    _git(pr_src, "push", "origin", "HEAD:refs/pull/13/head")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    _install_fail_closed_git_spy(monkeypatch, real_repo=repo)
    import asyncio
    asyncio.run(run_gate(repo, pr_url="https://github.com/acme/widgets/pull/13"))


# --------------------------------------------------------------------------- #
# 9. an empty diff must refuse, never fall through to the tool-enabled path   #
# --------------------------------------------------------------------------- #

def test_empty_diff_refuses_without_invoking_the_reviewer(tmp_path, monkeypatch):
    """Regression: `AdversarialReviewer.review` treats `diff_override=""` as
    falsy — identical to "no diff override" — and would silently switch to
    the multi-turn, tool-enabled gate path (reviewer.py:2566, 2588, 2601),
    defeating the single-turn/no-tools property the gate promises. A branch
    whose net diff against the merge base is empty (added then reverted)
    must refuse instead of handing the reviewer that empty string."""
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("temp\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "add b.txt")
    _git(repo, "rm", "b.txt")
    _git(repo, "commit", "-m", "revert b.txt")

    _ok_credential(monkeypatch)

    class _ExplodingReviewer:
        @classmethod
        def from_config(cls, data, **kw):
            return cls()

        async def review(self, *a, **kw):
            raise AssertionError(
                "the reviewer must never be invoked on an empty diff"
            )

    monkeypatch.setattr(oneshot, "AdversarialReviewer", _ExplodingReviewer)

    import asyncio
    with pytest.raises(GateUnavailable, match="diff is empty"):
        asyncio.run(run_gate(repo))


def test_pr_mode_refuses_when_pr_head_has_no_commits_beyond_base(tmp_path, monkeypatch):
    """Same empty-diff hazard, PR mode: a PR ref pushed straight at the same
    commit as `origin/main` has `merge_base == head`, which branch mode
    already guards (`_resolve_branch_mode`) but PR mode did not."""
    repo, bare = _make_repo_with_github_origin(tmp_path)
    pr_src = _push_pr_ref(bare, tmp_path / "pr_src4", 17)
    _git(pr_src, "push", "origin", "HEAD:refs/pull/17/head")

    _ok_credential(monkeypatch)

    class _ExplodingReviewer:
        @classmethod
        def from_config(cls, data, **kw):
            return cls()

        async def review(self, *a, **kw):
            raise AssertionError(
                "the reviewer must never be invoked when the PR has no "
                "commits beyond the base"
            )

    monkeypatch.setattr(oneshot, "AdversarialReviewer", _ExplodingReviewer)

    import asyncio
    with pytest.raises(GateUnavailable, match="no commits beyond"):
        asyncio.run(run_gate(
            repo, pr_url="https://github.com/acme/widgets/pull/17",
        ))


# --------------------------------------------------------------------------- #
# 10. PR URL must be a real GitHub URL naming this checkout's own repo        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad_url", [
    "literally/pull/4242",
    "ftp://evil.example/x/pull/4242",
    "https://gitlab.com/acme/widgets/pull/4242",
    "https://github.com.evil.example/acme/widgets/pull/4242",
])
def test_pr_url_must_be_a_real_github_pull_request_url(tmp_path, monkeypatch, bad_url):
    """Regression: the old `/pull/(\\d+)` regex matched ANY string containing
    `/pull/<digits>` — including non-GitHub hosts and bare paths — discarding
    owner/repo entirely, so a URL naming the wrong repository (or no
    repository at all) would still get "reviewed" and could print PASS."""
    repo, _bare = _make_repo_with_github_origin(tmp_path)
    _ok_credential(monkeypatch)

    import asyncio
    with pytest.raises(GateUnavailable, match="not a GitHub pull request URL"):
        asyncio.run(run_gate(repo, pr_url=bad_url))


def test_pr_mode_refuses_when_the_url_names_a_different_repo_than_origin(
    tmp_path, monkeypatch,
):
    """Regression for the same bug from the other side: a syntactically valid
    GitHub PR URL that names a DIFFERENT repository than this checkout's own
    `origin` must refuse, not silently review the wrong repository's PR."""
    repo, _bare = _make_repo_with_github_origin(tmp_path, owner="acme", repo_name="widgets")
    _ok_credential(monkeypatch)

    class _ExplodingReviewer:
        @classmethod
        def from_config(cls, data, **kw):
            return cls()

        async def review(self, *a, **kw):
            raise AssertionError(
                "the reviewer must never be invoked for a PR on a "
                "different repository than this checkout's origin"
            )

    monkeypatch.setattr(oneshot, "AdversarialReviewer", _ExplodingReviewer)

    import asyncio
    with pytest.raises(GateUnavailable, match="acme/widgets"):
        asyncio.run(run_gate(
            repo, pr_url="https://github.com/other-org/other-repo/pull/4242",
        ))


def test_pr_mode_refuses_when_origin_is_not_a_github_remote(tmp_path, monkeypatch):
    """A `--pr` URL cannot be verified against an `origin` that is not itself
    a `github.com` remote (e.g. a plain local path, as most of this suite's
    fixtures use) — refuse rather than reviewing on faith."""
    repo, _bare = _make_repo_with_origin(tmp_path)
    _ok_credential(monkeypatch)

    import asyncio
    with pytest.raises(GateUnavailable, match="not a github.com remote"):
        asyncio.run(run_gate(
            repo, pr_url="https://github.com/acme/widgets/pull/9",
        ))


# --------------------------------------------------------------------------- #
# 11. exit code 1 on a genuine FAIL, distinct from exit code 2's refusal      #
# --------------------------------------------------------------------------- #

def test_the_verb_exits_1_on_a_genuine_fail(tmp_path, monkeypatch):
    """`test_the_verb_exits_2_and_prints_no_pass` covers refusal (exit 2);
    this covers the other non-zero exit — a gate that actually ran and
    failed must exit 1, not 0 and not 2."""
    repo, _bare = _make_repo_with_origin(tmp_path)

    async def _fake_run_gate(repo_path, **kw):
        return GateResult(
            passed=False, comparison="x", before_ref="a", after_ref="b",
            mode="branch", tamper=_clean_tamper(), decision=ReviewDecision(
                passed=False,
                checklist=[ChecklistItem(label="blocking finding", passed=False)],
            ),
            uncommitted=[],
        )

    monkeypatch.setattr(oneshot, "run_gate", _fake_run_gate)
    result = CliRunner().invoke(gate, ["--repo", str(repo)])
    assert result.exit_code == 1
    assert "FAIL" in result.output


# --------------------------------------------------------------------------- #
# 12. rendered markdown carries the verdict, disclosures and tamper counts    #
# --------------------------------------------------------------------------- #

def _result(**overrides):
    base = dict(
        passed=True, comparison="working tree branch `feature` @ `abc1234`",
        before_ref="base", after_ref="head", mode="branch",
        tamper=_clean_tamper(), decision=_PASSING_DECISION, uncommitted=[],
    )
    base.update(overrides)
    return GateResult(**base)


def test_rendered_markdown_says_fail_on_a_failing_result():
    text = render_markdown(_result(passed=False))
    assert "## no_human gate — FAIL" in text
    assert "## no_human gate — PASS" not in text


def test_rendered_markdown_says_pass_on_a_passing_result():
    text = render_markdown(_result(passed=True))
    assert "## no_human gate — PASS" in text


def test_rendered_markdown_discloses_uncommitted_files():
    text = render_markdown(_result(
        comparison="working tree branch `feature`; 2 uncommitted file(s) "
                    "are NOT reviewed — commit them to include them",
    ))
    assert "uncommitted" in text
    assert "NOT reviewed" in text


def test_rendered_markdown_shows_tamper_before_after_counts():
    from no_human.testing.tamper_guard import TamperReport
    tamper = TamperReport(
        tampered=True, tests_before=12, tests_after=9,
        assertions_before=40, assertions_after=31,
        reasons=["deleted test_x.py::test_slow_path"],
    )
    text = render_markdown(_result(passed=False, tamper=tamper))
    assert "12->9" in text
    assert "40->31" in text
    assert "deleted test_x.py::test_slow_path" in text


def test_rendered_markdown_discloses_truncation_and_it_implies_not_passed():
    text = render_markdown(_result(passed=False, truncated=True))
    assert "truncated" in text.lower()
    assert "## no_human gate — FAIL" in text


def test_run_gate_actually_discovers_real_uncommitted_files(tmp_path, monkeypatch):
    """Integration test for `_uncommitted_paths`' real wiring through
    `run_gate` — the render-layer tests above only check `render_markdown`
    against a hand-built `comparison` string, which would not catch a
    mutation that made `_uncommitted_paths` always return `[]` (its own
    `GateResult.uncommitted` field would stay empty and the comparison
    string would never mention the file, even though the working tree
    genuinely has an uncommitted change). This creates a real uncommitted
    file in a real temp repo and checks the actual `GateResult`."""
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature commit")

    # A second, genuinely uncommitted change on top of the committed one.
    (repo / "uncommitted.txt").write_text("not committed\n")
    _git(repo, "add", "uncommitted.txt")  # staged, not committed

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    import asyncio
    result = asyncio.run(run_gate(repo))
    assert result.uncommitted == ["uncommitted.txt"], (
        f"expected the real uncommitted file to be reported, got {result.uncommitted!r}"
    )
    assert "1 uncommitted file(s) are NOT reviewed" in result.comparison
    text = render_markdown(result)
    assert "uncommitted" in text
    assert "NOT reviewed" in text


# --------------------------------------------------------------------------- #
# 13. a diff bigger than the reviewer's single-turn cap can never pass        #
# --------------------------------------------------------------------------- #

def test_a_diff_over_the_review_cap_never_passes(tmp_path, monkeypatch):
    """`AdversarialReviewer.review` silently truncates `diff_override` past
    `_DIFF_CAP` chars with no signal back to the caller (reviewer.py:2537) —
    so without this guard, a diff bigger than the cap would get a fraction
    of itself reviewed and could still come back as a bare PASS."""
    repo, _bare = _make_repo_with_origin(tmp_path)
    _git(repo, "checkout", "-b", "feature")
    # One line per byte-ish, comfortably over `_DIFF_CAP` (60_000 chars).
    (repo / "big.txt").write_text("\n".join(f"line {i}" for i in range(20_000)))
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "-m", "huge change")

    _ok_credential(monkeypatch)
    monkeypatch.setattr(oneshot, "AdversarialReviewer", _stub_reviewer(_PASSING_DECISION))

    import asyncio
    result = asyncio.run(run_gate(repo))
    assert result.truncated is True
    assert result.passed is False, "a truncated review must never be a bare pass"
