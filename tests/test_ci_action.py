"""Hermetic tests for the GitHub Action entry point (`no_human.ci_action`).

No network, no model, no `~/.no_human`. Every test either:
  - constructs a real temporary git repository with two commits and drives
    `run.main()` through real `git` subcommands, with `AdversarialReviewer`
    monkeypatched to a fake that returns a canned `ReviewDecision`, and
    `github.GitHubClient` monkeypatched to use `httpx.MockTransport`; or
  - calls a single small function (`_configure_credential`, `render_body`,
    `_cell`, `_truncate`, `github.GitHubClient._send`, ...) directly.

Exit-code contract under test throughout: 0 = ran and passed, or a documented
fork-skip. 1 = ran and found blocking findings/tamper (only when
`fail_on_findings` is true). 2 = did not run at all.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from no_human.ci_action import github, run
from no_human.review.reviewer import ReviewDecision, ReviewerUnavailable
from no_human.review.selfcheck import ChecklistItem
from no_human.testing.runner import TamperCheckUnavailable
from no_human.testing.tamper_guard import TamperReport


# --------------------------------------------------------------------------- #
# Repo fixture                                                                 #
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )
    return proc.stdout


@dataclass
class RepoInfo:
    path: Path
    base_sha: str
    head_sha: str

    def __fspath__(self) -> str:  # so `str(repo)` / Path(repo) both work
        return str(self.path)


@pytest.fixture
def repo(tmp_path: Path) -> RepoInfo:
    """A repo with two commits: adds `src/app.py` and `tests/bar.py`."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "t")
    (d / "src").mkdir()
    (d / "src" / "app.py").write_text("def f():\n    return 1\n")
    _git(d, "add", ".")
    _git(d, "commit", "-q", "-m", "base")
    base_sha = _git(d, "rev-parse", "HEAD").strip()

    (d / "tests").mkdir()
    (d / "tests" / "bar.py").write_text(
        "from src.app import f\n\n\n"
        "def test_a():\n    assert f() == 2\n\n\n"
        "def test_b():\n    assert f() != 3\n"
    )
    (d / "src" / "app.py").write_text("def f():\n    return 2\n")
    _git(d, "add", ".")
    _git(d, "commit", "-q", "-m", "head")
    head_sha = _git(d, "rev-parse", "HEAD").strip()

    return RepoInfo(path=d, base_sha=base_sha, head_sha=head_sha)


def _event(repo: RepoInfo, *, fork: bool = False, deleted_fork: bool = False,
           pr_number: int = 7, title: str = "Add a feature",
           body: str = "does the thing") -> dict:
    base_full = "acme/widgets"
    if deleted_fork:
        head_repo = None
    elif fork:
        head_repo = {"full_name": "someone-else/widgets"}
    else:
        head_repo = {"full_name": base_full}
    return {
        "repository": {"full_name": base_full},
        "pull_request": {
            "number": pr_number,
            "title": title,
            "body": body,
            "base": {"sha": repo.base_sha},
            "head": {"sha": repo.head_sha, "repo": head_repo},
        },
    }


@pytest.fixture
def env(tmp_path, monkeypatch, repo):
    """Base environment for a same-repo `pull_request` run. Returns a dict of
    paths for GITHUB_OUTPUT / GITHUB_STEP_SUMMARY so a test can read them back."""
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event(repo)))
    out_path = tmp_path / "output.txt"
    summary_path = tmp_path / "summary.md"
    out_path.write_text("")
    summary_path.write_text("")

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo.path))
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    monkeypatch.setenv("INPUT_CREDENTIAL", "sk-ant-api-testvalue")
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghp_testtoken")
    monkeypatch.delenv("INPUT_CREDENTIAL_MODE", raising=False)
    monkeypatch.delenv("INPUT_MAX_FILES", raising=False)
    monkeypatch.delenv("INPUT_FAIL_ON_FINDINGS", raising=False)
    monkeypatch.delenv("INPUT_DRY_RUN", raising=False)
    monkeypatch.delenv("INPUT_MODEL", raising=False)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return {"event_path": event_path, "out_path": out_path, "summary_path": summary_path, "repo": repo}


def _fake_reviewer(decision=None, exc: Exception | None = None):
    class _Fake:
        def __init__(self, *, model, **kw):
            self.model = model

        async def review(self, task, **kwargs):
            if exc is not None:
                raise exc
            return decision

    return _Fake


def _pass_decision() -> ReviewDecision:
    return ReviewDecision(passed=True, checklist=[])


def _mock_client(monkeypatch, handler):
    """Monkeypatch `run.github.GitHubClient` so every construction uses an
    `httpx.MockTransport` around *handler*, never a real socket."""

    class _Patched(github.GitHubClient):
        def __init__(self, *, token, api_url=github.DEFAULT_API_URL, **kw):
            super().__init__(token=token, api_url=api_url,
                              transport=httpx.MockTransport(handler), sleep=lambda s: None)

    monkeypatch.setattr(run.github, "GitHubClient", _Patched)


def _no_comments_then_create_handler(calls: list[tuple[str, str]], created_id: int = 1):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json=[], headers={})
        if request.method == "POST":
            return httpx.Response(201, json={"id": created_id, "body": ""})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler


# --------------------------------------------------------------------------- #
# Package boundary: no daemon, no store, no queueing CLI                       #
# --------------------------------------------------------------------------- #

_FORBIDDEN_IMPORT_SUBSTRINGS = (
    "core.orchestrator", "core.db", "core.store", "cli.commands",
)


def test_package_imports_no_store_or_daemon():
    pkg_dir = Path(run.__file__).parent
    for path in pkg_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = [module] + [f"{module}.{a.name}" for a in node.names]
            else:
                continue
            for name in names:
                for forbidden in _FORBIDDEN_IMPORT_SUBSTRINGS:
                    assert forbidden not in name, (
                        f"{path.name} imports {name!r}, which touches the "
                        f"forbidden {forbidden!r} surface"
                    )
            # A bare `import api` / `from . import api`-shaped forbidden name.
            if isinstance(node, ast.ImportFrom) and node.module == "no_human" and any(
                a.name == "api" for a in node.names
            ):
                pytest.fail(f"{path.name} imports the `api` package")


def test_git_is_the_only_subprocess_call_site_and_allowlist_is_read_only():
    source = (Path(run.__file__)).read_text(encoding="utf-8")
    assert source.count("subprocess.run(") == 1, (
        "run.py must call subprocess exactly once, inside `_git`"
    )
    for pkg_file in Path(run.__file__).parent.glob("*.py"):
        if pkg_file.name == "run.py":
            continue
        assert "subprocess.run(" not in pkg_file.read_text(encoding="utf-8")
    mutating = {"push", "commit", "merge", "checkout", "reset", "rebase", "clean", "branch", "tag"}
    assert run._ALLOWED_GIT_SUBCOMMANDS.isdisjoint(mutating)
    assert run._ALLOWED_GIT_SUBCOMMANDS == frozenset({"rev-parse", "merge-base", "diff"})


def test_git_helper_refuses_non_allowlisted_subcommand(repo):
    with pytest.raises(run.ActionError):
        run._git(repo.path, "push", "origin", "main")


def test_first_changed_line_finds_the_real_hunk_line_not_always_zero(repo):
    """`_first_changed_line` must return the REAL first-hunk line number for
    an ordinary modified file, not just its documented 0 fallback — an
    ablation that made this function always return 0 (degrading every
    citation to a bare path) would still pass every OTHER existing test,
    since none of them assert a nonzero line for a plain single-hunk
    modification."""
    line = run._first_changed_line(repo.path, repo.base_sha, repo.head_sha, "src/app.py")
    # `src/app.py` is modified (not newly added) in the head commit (see the
    # `repo` fixture): its one hunk is `@@ -1,2 +1,2 @@`, so the real
    # before-side start line is 1 — nonzero, and not a coincidence of the
    # 0-fallback.
    assert line == 1


def test_first_changed_line_returns_zero_for_unknown_path(repo):
    assert run._first_changed_line(repo.path, repo.base_sha, repo.head_sha, "no/such/file.py") == 0


# --------------------------------------------------------------------------- #
# Trust gate                                                                   #
# --------------------------------------------------------------------------- #


def test_pull_request_target_refused_even_with_valid_credential(env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    assert run.main() == run.EXIT_DID_NOT_RUN
    out = capsys.readouterr().out
    assert "pull_request_target" in out
    assert "::error::" in out
    # The refusal must name WHY, not just cite the trigger name — a
    # maintainer reading this in a job log needs the security reasoning,
    # not just "this event is unsupported".
    assert "privilege-escalation" in out


def test_workspace_head_must_match_pr_head_sha(env, monkeypatch, repo, capsys):
    """`actions/checkout` defaults to an ephemeral MERGE commit on
    `pull_request` events, not the PR's actual head. If the on-disk
    workspace's HEAD silently diverges from `pull_request.head.sha`, this
    Action would review/cite line numbers against the wrong tree. Pin that
    `main()` refuses to run rather than let that happen, and that a
    correctly-checked-out workspace (HEAD == head_sha, the common case in
    this test suite's own `env` fixture) is untouched by this check."""
    # A third commit moves HEAD without updating the event payload's
    # `head.sha` — simulating actions/checkout leaving an ephemeral merge
    # commit checked out.
    (repo.path / "src" / "app.py").write_text("def f():\n    return 3\n")
    _git(repo.path, "add", ".")
    _git(repo.path, "commit", "-q", "-m", "ephemeral merge commit")
    actual_head = _git(repo.path, "rev-parse", "HEAD").strip()
    assert actual_head != repo.head_sha

    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(exc=AssertionError("must not run")))
    assert run.main() == run.EXIT_DID_NOT_RUN
    out = capsys.readouterr().out
    assert actual_head in out
    assert repo.head_sha in out
    assert "ref: ${{ github.event.pull_request.head.sha }}" in out


def test_workspace_head_matching_pr_head_sha_is_not_blocked(env, monkeypatch):
    """Control for the check above: when the checkout's HEAD already equals
    `pull_request.head.sha` (the `env` fixture's normal state), the new
    HEAD-vs-head_sha guard must not fire a false positive."""
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))
    calls: list[tuple[str, str]] = []
    _mock_client(monkeypatch, _no_comments_then_create_handler(calls))
    assert run.main() == run.EXIT_OK


def test_unsupported_event_refused(env, monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    assert run.main() == run.EXIT_DID_NOT_RUN


def test_missing_event_path_refused(env, monkeypatch):
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert run.main() == run.EXIT_DID_NOT_RUN


@pytest.mark.parametrize("deleted_fork", [False, True])
def test_fork_pr_is_skipped_not_reviewed(env, monkeypatch, deleted_fork, capsys):
    event_path = env["event_path"]
    event_path.write_text(json.dumps(_event(env["repo"], fork=not deleted_fork, deleted_fork=deleted_fork)))
    calls: list[tuple[str, str]] = []
    _mock_client(monkeypatch, lambda req: calls.append((req.method, str(req.url))) or httpx.Response(200, json=[]))
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(exc=AssertionError("must not be called")))

    assert run.main() == run.EXIT_OK
    assert calls == [], "a fork skip must make zero GitHub API calls"
    out = capsys.readouterr().out
    assert "skipping" in out.lower()


def test_same_repo_pr_proceeds_to_review(env, monkeypatch):
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))
    calls: list[tuple[str, str]] = []
    _mock_client(monkeypatch, _no_comments_then_create_handler(calls))
    assert run.main() == run.EXIT_OK
    assert ("POST", ) != () and any(m == "POST" for m, _ in calls)


# --------------------------------------------------------------------------- #
# Credential handling                                                         #
# --------------------------------------------------------------------------- #


def test_missing_credential_fails_loudly_naming_it(env, monkeypatch, capsys):
    monkeypatch.setenv("INPUT_CREDENTIAL", "")
    assert run.main() == run.EXIT_DID_NOT_RUN
    out = capsys.readouterr().out
    assert "credential" in out.lower()


def test_missing_github_token_fails_loudly(env, monkeypatch, capsys):
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a missing github_token must make zero GitHub API calls")

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_DID_NOT_RUN
    out = capsys.readouterr().out
    assert "::error::" in out
    assert "github_token" in out.lower() or "token" in out.lower()


@pytest.mark.parametrize(
    "raw, expected_mode",
    [
        ("sk-ant-oat-abc123", "oauth"),
        ("sk-ant-api-abc123", "api_key"),
        ("sk-ant-somethingelse", "api_key"),
    ],
)
def test_credential_auto_detection(monkeypatch, raw, expected_mode):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cred = run._configure_credential(raw, "auto")
    assert cred.mode == expected_mode


def test_credential_auto_detection_rejects_unrecognized_shape(monkeypatch):
    with pytest.raises(run.ActionError):
        run._configure_credential("totally-not-a-claude-credential", "auto")


def test_credential_mode_rejects_bogus_explicit_value():
    with pytest.raises(run.ActionError):
        run._configure_credential("sk-ant-api-x", "bogus")


def test_api_key_mode_scrubs_oauth_token_and_profiles(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "old-oauth")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_WORK", "old-oauth-work")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "some-redirect")
    cred = run._configure_credential("sk-ant-api-new", "api_key")
    assert cred.mode == "api_key"
    assert os.environ[run.API_KEY_VAR] == "sk-ant-api-new"
    assert run.SUBSCRIPTION_TOKEN_VAR not in os.environ
    assert "CLAUDE_CODE_OAUTH_TOKEN_WORK" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


def test_oauth_mode_scrubs_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-api-key")
    cred = run._configure_credential("sk-ant-oat-new", "oauth")
    assert cred.mode == "oauth"
    assert os.environ[run.SUBSCRIPTION_TOKEN_VAR] == "sk-ant-oat-new"
    assert run.API_KEY_VAR not in os.environ


def test_oauth_mode_scrubs_profile_suffixed_oauth_vars(monkeypatch):
    """`api_key` mode scrubs `CLAUDE_CODE_OAUTH_TOKEN_<PROFILE>` vars via
    `_pop_oauth_bare_and_profiles` (see test_api_key_mode_scrubs_...). The
    OAUTH branch must scrub the SAME profile-suffixed leftovers too — a
    stale `CLAUDE_CODE_OAUTH_TOKEN_WORK` from a previous run/profile must
    not coexist with the new bare token this run is about to set, since
    some SDK paths prefer a profile-suffixed var over the bare one."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_WORK", "stale-oauth-work")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_PERSONAL", "stale-oauth-personal")
    cred = run._configure_credential("sk-ant-oat-new", "oauth")
    assert cred.mode == "oauth"
    assert os.environ[run.SUBSCRIPTION_TOKEN_VAR] == "sk-ant-oat-new"
    assert "CLAUDE_CODE_OAUTH_TOKEN_WORK" not in os.environ
    assert "CLAUDE_CODE_OAUTH_TOKEN_PERSONAL" not in os.environ


def test_credential_value_never_appears_unmasked(env, monkeypatch, capsys):
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))
    calls: list[tuple[str, str]] = []
    _mock_client(monkeypatch, _no_comments_then_create_handler(calls))
    run.main()
    out = capsys.readouterr().out
    credential = "sk-ant-api-testvalue"
    # The ONE sanctioned appearance is the ::add-mask:: directive itself.
    assert out.count(credential) == 1
    assert f"::add-mask::{credential}" in out


# --------------------------------------------------------------------------- #
# Cost bound                                                                  #
# --------------------------------------------------------------------------- #


def test_max_files_must_be_a_positive_integer(env, monkeypatch):
    monkeypatch.setenv("INPUT_MAX_FILES", "not-a-number")
    assert run.main() == run.EXIT_DID_NOT_RUN


def test_max_files_zero_is_rejected(env, monkeypatch):
    monkeypatch.setenv("INPUT_MAX_FILES", "0")
    assert run.main() == run.EXIT_DID_NOT_RUN


def test_max_files_caps_reviewed_file_count(env, monkeypatch):
    monkeypatch.setenv("INPUT_MAX_FILES", "1")
    seen = {}

    class _Fake:
        def __init__(self, *, model, **kw):
            pass

        async def review(self, task, **kwargs):
            seen["diff"] = kwargs["diff_override"]
            return _pass_decision()

    monkeypatch.setattr(run, "AdversarialReviewer", _Fake)
    calls: list[tuple[str, str]] = []
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_OK
    # Two files changed (src/app.py, tests/bar.py); only 1 is in scope.
    assert "app.py" in seen["diff"] or "bar.py" in seen["diff"]
    assert not ("app.py" in seen["diff"] and "bar.py" in seen["diff"])
    assert "capped by `max_files`" in bodies[0]["body"]


def test_non_ascii_filename_is_not_silently_dropped_from_the_diff(env, monkeypatch, repo):
    """`git diff --name-only` C-quotes any path with non-ASCII bytes by
    default (e.g. `régression.py` comes back as the STRING
    `"r\\303\\251gression.py"`), and a quoted string does not match anything
    as a pathspec. Without `-z` (NUL-terminated, unquoted output), such a
    file silently vanishes from `diff_override` while the rendered comment
    still claims every changed file was reviewed — exactly the defect class
    this Action exists to catch, now happening to itself. In the degenerate
    case where EVERY changed path is quoted, `diff_override` becomes ""
    (falsy), which would send `AdversarialReviewer.review` down its
    no-override branch — recomputing the diff itself over every file and
    silently ignoring `max_files`."""
    evil = repo.path / "régression.py"
    evil.write_text("def backdoor(cmd):\n    import os\n    os.system(cmd)\n")
    _git(repo.path, "add", ".")
    _git(repo.path, "commit", "-q", "-m", "add a non-ascii filename")
    new_head = _git(repo.path, "rev-parse", "HEAD").strip()

    event = _event(repo)
    event["pull_request"]["head"]["sha"] = new_head
    env["event_path"].write_text(json.dumps(event))

    seen = {}

    class _Fake:
        def __init__(self, *, model, **kw):
            pass

        async def review(self, task, **kwargs):
            seen["diff"] = kwargs["diff_override"]
            return _pass_decision()

    monkeypatch.setattr(run, "AdversarialReviewer", _Fake)
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_OK
    assert "diff" in seen and seen["diff"], "diff_override must not be empty/falsy"
    assert "backdoor" in seen["diff"], "the non-ASCII-named file must not be dropped from the reviewed diff"
    assert "Files reviewed: 3 of 3" in bodies[0]["body"]


def test_changed_paths_survive_git_c_quoting(repo):
    """Unit-level pin on the `-z`/NUL-split fix directly: a path with a byte
    git would otherwise C-quote in `--name-only` output must come back
    identical to the on-disk filename, not the quoted representation."""
    evil = repo.path / "quöted name.py"
    evil.write_text("x = 1\n")
    _git(repo.path, "add", ".")
    _git(repo.path, "commit", "-q", "-m", "quoted path")
    new_head = _git(repo.path, "rev-parse", "HEAD").strip()

    raw = run._git(repo.path, "diff", "-z", "--name-only", f"{repo.head_sha}..{new_head}")
    changed = [p for p in raw.split("\0") if p]
    assert "quöted name.py" in changed
    assert not any(p.startswith('"') for p in changed)


def test_set_output_write_failure_does_not_raise(tmp_path, monkeypatch):
    """A GITHUB_OUTPUT write failure (e.g. the runner-owned-file/permission
    shape reproduced against the real Docker image, where this process
    cannot write the path GitHub handed it) must never propagate as an
    uncaught exception. `sys.exit(main())` would let that fall through to
    Python's default unhandled-exception exit code, 1 — the SAME code this
    module uses for EXIT_FINDINGS ("ran and found blocking findings") — so a
    pure infrastructure failure would be indistinguishable, by exit code
    alone, from a real review verdict. `_append_step_summary` already
    swallows this class of failure for GITHUB_STEP_SUMMARY; `_set_output`
    must match."""
    unwritable = tmp_path / "not-a-real-directory" / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(unwritable))
    run._set_output("verdict", "PASS")  # must not raise


def test_no_changed_files_is_a_synthetic_pass_without_reviewer_or_tamper(env, monkeypatch, repo):
    # A PR whose base == head has an empty diff.
    event = _event(repo)
    event["pull_request"]["base"]["sha"] = repo.head_sha
    env["event_path"].write_text(json.dumps(event))
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(exc=AssertionError("must not run")))
    monkeypatch.setattr(run, "tamper_check_between", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run")))
    calls: list[tuple[str, str]] = []
    _mock_client(monkeypatch, _no_comments_then_create_handler(calls))
    assert run.main() == run.EXIT_OK


# --------------------------------------------------------------------------- #
# Tamper guard                                                                #
# --------------------------------------------------------------------------- #


def test_tamper_guard_unavailable_fails_closed(env, monkeypatch):
    monkeypatch.setattr(run, "tamper_check_between", lambda *a, **kw: (_ for _ in ()).throw(TamperCheckUnavailable("no git")))
    assert run.main() == run.EXIT_DID_NOT_RUN


def test_tamper_findings_render_with_file_line_citations(env, monkeypatch, repo):
    report = TamperReport(
        tampered=True, tests_before=2, tests_after=1,
        assertions_before=2, assertions_after=1,
        reasons=[
            "test file deleted: tests/gone.py",
            "tests/bar.py: assertions 2->1 (test neutered)",
        ],
    )
    monkeypatch.setattr(run, "tamper_check_between", lambda *a, **kw: report)
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))
    calls: list[tuple[str, str]] = []
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_FINDINGS
    body = bodies[0]["body"]
    assert "❌ FAIL" in body
    assert "tests/gone.py" in body
    assert "tests/bar.py:" in body  # deleted-file line=1 vs a real hunk line


# --------------------------------------------------------------------------- #
# Review outcomes and exit codes                                              #
# --------------------------------------------------------------------------- #


def test_clean_pass(env, monkeypatch):
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))
    calls: list[tuple[str, str]] = []
    _mock_client(monkeypatch, _no_comments_then_create_handler(calls))
    assert run.main() == run.EXIT_OK


def test_blocking_findings_fail_the_gate_by_default(env, monkeypatch):
    item = ChecklistItem(label="bug", passed=False, file="src/app.py", line=2,
                          comment="off-by-one", severity="high")
    decision = ReviewDecision(passed=False, checklist=[item])
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(decision))
    calls: list[tuple[str, str]] = []
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_FINDINGS
    assert "src/app.py:2" in bodies[0]["body"]


def test_fail_on_findings_false_still_reports_fail_but_exits_zero(env, monkeypatch):
    item = ChecklistItem(label="bug", passed=False, file="src/app.py", line=2,
                          comment="off-by-one", severity="high")
    decision = ReviewDecision(passed=False, checklist=[item])
    monkeypatch.setenv("INPUT_FAIL_ON_FINDINGS", "false")
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(decision))
    calls: list[tuple[str, str]] = []
    _mock_client(monkeypatch, _no_comments_then_create_handler(calls))
    assert run.main() == run.EXIT_OK


def test_reviewer_veto_with_no_blocking_items_still_fails_the_gate(env, monkeypatch):
    """`decision.passed` can be False from `_gate_verdict` (goal unreachable,
    spec_compliance failed, or an empty checklist) without a single item
    landing in `blocking_items`. Reading only `blocking`/`tampered` would
    silently render PASS here even though the reviewer's own verdict is
    FAIL — pin that this Action folds `decision.passed` into its verdict."""
    decision = ReviewDecision(passed=False, checklist=[])
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(decision))
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_FINDINGS
    assert "❌ FAIL" in bodies[0]["body"]


def test_reviewer_veto_with_failing_but_nonblocking_items_still_fails_the_gate(env, monkeypatch):
    """A checklist of only low/nit findings never populates `blocking_items`,
    but the reviewer can still veto the whole review (`passed=False`) — e.g.
    it disagrees with its own checklist. That veto must still fail the gate."""
    nit = ChecklistItem(label="nit", passed=False, file="x.py", line=1,
                         comment="style", severity="low")
    decision = ReviewDecision(passed=False, checklist=[nit])
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(decision))
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_FINDINGS
    assert "❌ FAIL" in bodies[0]["body"]


def test_reviewer_unavailable_means_did_not_run(env, monkeypatch):
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(exc=ReviewerUnavailable("no verdict")))
    assert run.main() == run.EXIT_DID_NOT_RUN


def test_reviewer_unexpected_exception_means_did_not_run(env, monkeypatch):
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(exc=RuntimeError("boom")))
    assert run.main() == run.EXIT_DID_NOT_RUN


def test_transport_error_decision_means_did_not_run(env, monkeypatch):
    """`transport_error=True` means the reviewer session errored or never
    returned a result — the SAME "did not run" outcome as a raised
    `ReviewerUnavailable`, even though this decision otherwise looks
    checklist-shaped (`passed=False`, empty checklist). Assert zero GitHub
    API calls too: a transport-errored decision must never reach the
    upsert path and post a comment, which would look like a real verdict."""
    decision = ReviewDecision(passed=False, checklist=[], transport_error=True)
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(decision))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a transport-errored decision must make zero GitHub API calls")

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_DID_NOT_RUN


def test_reviewer_veto_isolates_blocking_or_from_tampered_and_passed(env, monkeypatch):
    """Isolate the FIRST disjunct of `verdict = "FAIL" if (blocking or
    tampered or not decision.passed) else "PASS"`: a checklist item graded
    blocking must fail the gate even when `decision.passed=True` (the
    reviewer's own top-level verdict disagrees with its checklist) and the
    tamper guard is clean — so an ablation that deleted the `blocking or`
    term specifically (leaving only `tampered or not decision.passed`)
    would still show green here without this test."""
    item = ChecklistItem(label="bug", passed=False, file="src/app.py", line=2,
                          comment="off-by-one", severity="high")
    decision = ReviewDecision(passed=True, checklist=[item])
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(decision))
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_FINDINGS
    assert "❌ FAIL" in bodies[0]["body"]
    assert "src/app.py:2" in bodies[0]["body"]


def test_tampered_true_with_no_reasons_still_fails_via_or_tampered(env, monkeypatch):
    """Isolate the SECOND disjunct: an aggregate-tampered report with an
    EMPTY `reasons` list produces zero `tamper_items`, so `blocking` stays
    exactly `decision.blocking_items` (empty here) and only `tampered`
    itself carries the fail signal. An ablation that dropped the bare
    `tampered or` term would still show green here without this test."""
    report = TamperReport(
        tampered=True, tests_before=2, tests_after=1,
        assertions_before=2, assertions_after=1, reasons=[],
    )
    monkeypatch.setattr(run, "tamper_check_between", lambda *a, **kw: report)
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_FINDINGS
    assert "❌ FAIL" in bodies[0]["body"]
    assert "TAMPERED" in bodies[0]["body"]


def test_net_zero_cross_file_tamper_reasons_are_advisory_not_blocking(env, monkeypatch):
    """`tamper_report.tampered=False` (the guard's own AGGREGATE verdict)
    with a non-empty `reasons` list (e.g. a net-zero move of an assertion
    from one file to another during an ordinary refactor) must render those
    reasons as ADVISORY context, never as a blocking failure — the
    aggregate, not the per-file free text, is the fail signal. This pins
    the caller-side half of that routing (the `if tampered: ... else: ...`
    branch in `main`), independent of `_tamper_checklist_items`'s own
    severity-mirroring."""
    report = TamperReport(
        tampered=False, tests_before=2, tests_after=2,
        assertions_before=4, assertions_after=4,
        reasons=["tests/bar.py: assertions 3->2 (moved to tests/other.py)"],
    )
    monkeypatch.setattr(run, "tamper_check_between", lambda *a, **kw: report)
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_OK
    body = bodies[0]["body"]
    assert "✅ PASS" in body
    # The blocking table is empty ("None.") ...
    blocking_idx = body.find("### Blocking findings")
    advisory_idx = body.find("Advisory findings")
    assert blocking_idx != -1 and advisory_idx != -1
    assert "None." in body[blocking_idx:advisory_idx]
    # ... and the reason string landed in the advisory section instead.
    assert "moved to tests/other.py" in body[advisory_idx:]


def test_github_api_url_env_var_is_honored_for_ghes(env, monkeypatch):
    """On GitHub Enterprise Server, `GITHUB_API_URL` is set by the runner to
    the GHES host's own REST API (not api.github.com). Pin that `main`
    reads it and threads it into `github.GitHubClient(api_url=...)` — an
    Action that hard-codes `github.DEFAULT_API_URL` would silently call the
    wrong host (and, for a GHES instance behind a firewall, simply fail to
    connect) on every GHES-hosted run."""
    monkeypatch.setenv("GITHUB_API_URL", "https://ghes.example.com/api/v3")
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 1, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_OK
    assert seen_urls, "expected at least one GitHub API call"
    assert all(url.startswith("https://ghes.example.com/api/v3") for url in seen_urls)


def test_comment_url_output_honors_github_server_url_for_ghes(env, monkeypatch):
    """`comment_url` is a human-facing link, built from `repo_full`/`pr_number`
    and the posted comment's id rather than returned by the API. On GitHub
    Enterprise Server, hardcoding `https://github.com` here would produce a
    dead link — the runner sets `GITHUB_SERVER_URL` to the GHES host's own web
    origin (distinct from `GITHUB_API_URL`, its REST API origin, already
    covered by test_github_api_url_env_var_is_honored_for_ghes above), and
    `_post_and_exit` must build the URL from that instead of a literal."""
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://ghes.example.com")
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"id": 42, "body": ""})

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_OK
    outputs = env["out_path"].read_text(encoding="utf-8")
    assert "comment_url=https://ghes.example.com/" in outputs
    assert "#issuecomment-42" in outputs


def test_dry_run_makes_no_http_calls(env, monkeypatch, capsys):
    monkeypatch.setenv("INPUT_DRY_RUN", "true")
    monkeypatch.setattr(run, "AdversarialReviewer", _fake_reviewer(_pass_decision()))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry_run must make zero GitHub API calls")

    _mock_client(monkeypatch, handler)
    assert run.main() == run.EXIT_OK
    out = capsys.readouterr().out
    assert run.MARKER in out


# --------------------------------------------------------------------------- #
# Rendering helpers                                                           #
# --------------------------------------------------------------------------- #


def test_cell_collapses_newlines_and_escapes_pipes():
    assert run._cell("a\nb|c\r\nd") == "a b\\|c d"


def test_truncate_drops_advisory_before_hard_truncating():
    advisory = [ChecklistItem(label="nit", passed=False, file="x.py", line=1,
                               comment="x" * 500, severity="low")]
    body = run.render_body(
        verdict="PASS", blocking=[], advisory=advisory, demoted_citations=[],
        model="m", files_total=1, files_reviewed=1, diff_capped=False,
        credential_mode="api_key", tampered=False,
    )
    assert "advisory" in body.lower() or "Advisory" in body
    huge_advisory = [
        ChecklistItem(label="nit", passed=False, file="x.py", line=i, comment="y" * 200, severity="low")
        for i in range(2000)
    ]
    huge_body = run.render_body(
        verdict="PASS", blocking=[], advisory=huge_advisory, demoted_citations=[],
        model="m", files_total=1, files_reviewed=1, diff_capped=False,
        credential_mode="api_key", tampered=False,
    )
    assert len(huge_body) <= run._BODY_CAP
    assert "omitted for length" in huge_body


# --------------------------------------------------------------------------- #
# github.py: write-surface enforcement, comment upsert, status-code handling  #
# --------------------------------------------------------------------------- #


def test_write_surface_violation_blocks_disallowed_paths():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, json={})

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(github.WriteSurfaceViolation):
        client._send("POST", "/repos/o/r/pulls/1/merge")
    with pytest.raises(github.WriteSurfaceViolation):
        client._send("PATCH", "/repos/o/r/pulls/1")
    with pytest.raises(github.WriteSurfaceViolation):
        client._send("POST", "/repos/o/r/pulls/1/reviews")
    # PUT /.../pulls/{n}/merge is the actual merge endpoint, and DELETE would
    # remove a comment or a ref — both must be refused exactly like the
    # POST/PATCH cases above. A one-change-at-a-time ablation (an early
    # `return` for either verb in `_assert_write_allowed`) must turn these red.
    with pytest.raises(github.WriteSurfaceViolation):
        client._send("PUT", "/repos/o/r/pulls/1/merge")
    with pytest.raises(github.WriteSurfaceViolation):
        client._send("DELETE", "/repos/o/r/issues/comments/9")
    with pytest.raises(github.WriteSurfaceViolation):
        client._send("DELETE", "/repos/o/r/git/refs/heads/main")
    assert calls == [], "a refused write must never reach the transport"


def test_write_surface_allows_comment_endpoints_including_query_strings():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    client._send("GET", "/repos/o/r/issues/1/comments?per_page=100")


def test_upsert_creates_when_no_marked_comment_exists():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 5, "body": "unrelated"}])
        return httpx.Response(201, json={"id": 9, "body": "new"})

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    c = github.upsert_comment(client, "o/r", 1, run.MARKER, "new")
    assert c.id == 9
    assert [m for m, _ in calls] == ["GET", "POST"]


def test_upsert_updates_in_place_never_duplicates():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": 3, "body": f"{run.MARKER}\nold"}])
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": 3, "body": "updated"})
        raise AssertionError("must not POST when a marked comment already exists")

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    c = github.upsert_comment(client, "o/r", 1, run.MARKER, "updated")
    assert c.id == 3
    assert [m for m, _ in calls] == ["GET", "PATCH"]


def test_find_marked_comment_picks_lowest_id_among_duplicates():
    comments = [
        github.Comment(id=42, body=f"{run.MARKER}\nsecond"),
        github.Comment(id=7, body=f"{run.MARKER}\nfirst"),
        github.Comment(id=100, body="unrelated"),
    ]
    found = github.find_marked_comment(comments, run.MARKER)
    assert found is not None and found.id == 7


@pytest.mark.parametrize(
    "status, body_text, headers, expect_message",
    [
        (401, "", {}, "401"),
        (404, "", {}, "404"),
        (410, "", {}, "410 Gone"),
        (403, "Resource not accessible by integration", {}, "pull-requests: write"),
    ],
)
def test_send_raises_named_errors_without_retry(status, body_text, headers, expect_message):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, text=body_text, headers=headers)

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(github.GitHubAPIError, match=expect_message):
        client._send("GET", "/repos/o/r/issues/1/comments")
    assert len(calls) == 1


def test_send_refuses_to_follow_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://evil.example/"})

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(github.GitHubAPIError, match="redirected"):
        client._send("GET", "/repos/o/r/issues/1/comments")


def test_send_retries_5xx_then_raises():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    with pytest.raises(github.GitHubAPIError, match="503"):
        client._send("GET", "/repos/o/r/issues/1/comments")
    assert len(calls) == 1 + github._MAX_5XX_RETRIES


def test_send_backs_off_and_succeeds_on_429_then_200():
    calls = []
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json=[])

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=slept.append)
    resp = client._send("GET", "/repos/o/r/issues/1/comments")
    assert resp.status_code == 200
    assert len(calls) == 2
    assert slept == [1.0]


def test_send_truncates_body_once_on_422_then_retries():
    seen_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_bodies.append(len(payload["body"]))
        if len(seen_bodies) == 1:
            return httpx.Response(422)
        return httpx.Response(201, json={"id": 1, "body": payload["body"]})

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    big_body = "x" * (github.MAX_BODY_CHARS + 500)
    resp = client._send("POST", "/repos/o/r/issues/1/comments", json={"body": big_body})
    assert resp.status_code == 201
    assert seen_bodies[0] == len(big_body)
    assert seen_bodies[1] == github.MAX_BODY_CHARS


def test_upsert_relists_and_patches_on_create_failure_duplicate_hazard():
    """A POST that raises may still have created the comment server-side —
    the upsert must re-list and PATCH rather than leaving the caller to retry
    into a duplicate POST."""
    calls = []
    post_attempted = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            if post_attempted["n"] == 0:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"id": 55, "body": f"{run.MARKER}\nlanded"}])
        if request.method == "POST":
            post_attempted["n"] += 1
            return httpx.Response(500)
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": 55, "body": "updated"})
        raise AssertionError(request.method)

    client = github.GitHubClient(token="t", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    c = github.upsert_comment(client, "o/r", 1, run.MARKER, "updated")
    assert c.id == 55
    assert calls.count("POST") == 1 + github._MAX_5XX_RETRIES
    assert calls[-1] == "PATCH"
