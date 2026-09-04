"""`GitRepo.ls_remote_exact` — the exact-ref remote-sha read that
`Orchestrator._run_attempt` pins ONCE, before the coder session starts, and
that `Orchestrator._base_exclusion_refs` later trusts instead of re-reading
the remote at gate time.

Exact-match is the load-bearing property under test: `git ls-remote <remote>
<ref>` matches on *tail* path components, so a repo carrying a ref shaped
like `refs/heads/x/refs/heads/develop` would otherwise surface a decoy sha
for a caller asking about `refs/heads/develop`. Every test here either
proves the exact-match filter holds, or proves the method fails closed
(returns `None`) on every other kind of unreadable state — never raises,
never guesses.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from no_human.vcs.git import GitRepo


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=check,
    )


@pytest.fixture
def bare_origin(tmp_path):
    """A bare remote with `develop` at a known sha, plus a local clone wired
    to it as `origin` — the rig `GitRepo.ls_remote_exact` is exercised
    against."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))

    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "f.txt").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "main")

    _git(work, "checkout", "-qb", "develop")
    (work / "f.txt").write_text("develop\n")
    _git(work, "commit", "-qam", "on develop")
    _git(work, "push", "-q", "origin", "develop")
    develop_sha = _git(work, "rev-parse", "develop").stdout.strip()

    return {"bare": bare, "work": work, "develop_sha": develop_sha}


def test_exact_ref_returns_the_advertised_sha(bare_origin):
    repo = GitRepo(bare_origin["work"])
    sha = repo.ls_remote_exact("refs/heads/develop")
    assert sha == bare_origin["develop_sha"]


def test_absent_branch_returns_none(bare_origin):
    repo = GitRepo(bare_origin["work"])
    assert repo.ls_remote_exact("refs/heads/does-not-exist") is None


def test_decoy_suffix_matching_ref_is_not_returned(bare_origin):
    """`git ls-remote origin refs/heads/develop` also matches a ref like
    `refs/heads/x/refs/heads/develop` (tail-path matching) — a decoy sha for
    a ref name that only coincidentally ends the same way. The exact-match
    filter must ignore that line entirely and fall back to the real
    `refs/heads/develop` line, not the decoy's sha."""
    work = bare_origin["work"]
    decoy_ref = "refs/heads/x/refs/heads/develop"
    _git(work, "update-ref", decoy_ref, "HEAD")
    _git(work, "push", "-q", "origin", f"{decoy_ref}:{decoy_ref}")

    repo = GitRepo(work)
    sha = repo.ls_remote_exact("refs/heads/develop")
    assert sha == bare_origin["develop_sha"], (
        "a tail-matching decoy ref must not override the exact match"
    )


def test_decoy_only_no_exact_match_returns_none(tmp_path):
    """When the ONLY line `ls-remote` returns is a tail-match decoy — no ref
    exactly equal to the one asked for — the method must return `None`, not
    the decoy's sha."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "f.txt").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "main")

    decoy_ref = "refs/heads/x/refs/heads/develop"
    _git(work, "update-ref", decoy_ref, "HEAD")
    _git(work, "push", "-q", "origin", f"{decoy_ref}:{decoy_ref}")

    repo = GitRepo(work)
    assert repo.ls_remote_exact("refs/heads/develop") is None


def _plain_repo(tmp_path) -> Path:
    work = tmp_path / "plain"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "f.txt").write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    return work


@pytest.mark.parametrize("bad_ref", [
    "develop",                 # not a full ref
    "-oProxyCommand=x",        # option-injection shaped
    "--upload-pack=x",
])
def test_malformed_ref_is_refused_before_any_subprocess_runs(
        monkeypatch, tmp_path, bad_ref):
    work = _plain_repo(tmp_path)
    called = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: called.append((a, k)),
    )
    repo = GitRepo(work)
    assert repo.ls_remote_exact(bad_ref) is None
    assert called == [], "subprocess.run must never be reached for a malformed ref"


def test_unreachable_remote_returns_none(tmp_path):
    work = _plain_repo(tmp_path)
    _git(work, "remote", "add", "origin", "https://127.0.0.1:1/nope.git")
    repo = GitRepo(work)
    assert repo.ls_remote_exact("refs/heads/main") is None


def test_timeout_returns_none(monkeypatch, tmp_path):
    work = _plain_repo(tmp_path)
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)
    monkeypatch.setattr(subprocess, "run", _boom)
    repo = GitRepo(work)
    assert repo.ls_remote_exact("refs/heads/main") is None


def test_oserror_returns_none(monkeypatch, tmp_path):
    work = _plain_repo(tmp_path)
    def _boom(*a, **k):
        raise OSError("git not found")
    monkeypatch.setattr(subprocess, "run", _boom)
    repo = GitRepo(work)
    assert repo.ls_remote_exact("refs/heads/main") is None


def test_latency_against_a_local_bare_origin_is_under_100ms(bare_origin):
    """Not a hard perf gate (CI variance), but pins the design claim that a
    single exact-ref `ls-remote` against a local remote is cheap enough to
    run once per attempt without becoming the bottleneck — measured, not
    assumed. See PR body for the measured number this test asserts against."""
    repo = GitRepo(bare_origin["work"])
    start = time.monotonic()
    sha = repo.ls_remote_exact("refs/heads/develop")
    elapsed_ms = (time.monotonic() - start) * 1000
    assert sha == bare_origin["develop_sha"]
    assert elapsed_ms < 100, f"ls_remote_exact took {elapsed_ms:.1f}ms locally"
