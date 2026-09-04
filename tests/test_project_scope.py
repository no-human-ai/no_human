"""Project scoping for lessons (B4): identity = sha256 of the normalized
remote URL.

Every lesson row was keyed on the checkout PATH, so the same repository
cloned at two paths — including the product's own per-task worktrees — read
as two unrelated projects. B4 keys recall on the repository's remote
identity: ``"prj:" + sha256(normalized remote URL)``, with credentials
stripped BEFORE hashing so nothing credential-shaped is ever persisted.

What must stay true:
  * two remotes differing only by the ``.git`` suffix (or credentials, or
    trailing slash, or host case, or transport) are the SAME scope,
  * a credential-bearing remote URL is never stored raw anywhere — the raw
    URL cannot be recovered from anything the database file contains,
  * recall surfaces the current project's lessons (across checkouts) plus
    explicitly-global ones, and NOT another project's,
  * legacy path-keyed rows still load, and are upgraded to the scope the
    next time their repo is seen (`stamp_project_scope`).
"""

from __future__ import annotations

import hashlib
import subprocess

import pytest
import pytest_asyncio

from no_human.core.db import Store
from no_human.core.task import Task
from no_human.learning import LearningQueue
from no_human.learning.queue import _project_scope_of
from no_human.learning.scope import (
    SCOPE_PREFIX,
    normalize_remote_url,
    project_scope_id,
    repo_root,
    resolve_project_scope,
)

CLEAN_URL = "https://example.com/team/repo.git"
CRED_URL = "https://alice:s3kr1t-token@example.com/team/repo.git"


def _git_repo(path, remote_url=None):
    """A real git repo at *path*, optionally with an `origin` remote — the
    resolution path under test shells out to real git."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if remote_url:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                        remote_url], check=True)
    return str(path)


# ── normalization: one repository, one identity ───────────────────────────── #

def test_the_git_suffix_does_not_split_a_project():
    """The required pair: two remotes differing ONLY by `.git` map to the
    same scope."""
    assert (project_scope_id("https://example.com/team/repo.git")
            == project_scope_id("https://example.com/team/repo"))


def test_credentials_host_case_slash_and_transport_do_not_split_a_project():
    canonical = project_scope_id(CLEAN_URL)
    assert canonical is not None
    for variant in (
        CRED_URL,                                  # credentials stripped
        "https://Example.COM/team/repo.git",       # host lowercased
        "https://example.com/team/repo.git/",      # trailing slash
        "https://example.com/team/repo/",          # both
        "git@example.com:team/repo.git",           # scp-like ssh
        "ssh://git@example.com/team/repo",         # ssh URL
    ):
        assert project_scope_id(variant) == canonical, variant


def test_the_path_segment_keeps_its_case():
    """Only the host is case-insensitive by standard; lowercasing paths would
    merge repos some forges keep distinct."""
    assert (project_scope_id("https://example.com/Team/Repo")
            != project_scope_id("https://example.com/team/repo"))


def test_normalization_shapes():
    assert normalize_remote_url(CRED_URL) == "example.com/team/repo"
    assert normalize_remote_url("git@example.com:team/repo.git") == (
        "example.com/team/repo")
    assert normalize_remote_url("/srv/git/repo.git") == "/srv/git/repo"
    assert normalize_remote_url("file:///srv/git/repo") == "/srv/git/repo"
    assert normalize_remote_url("") == ""
    assert normalize_remote_url(None) == ""
    assert project_scope_id("") is None


def test_the_scope_is_a_fixed_shape_digest_not_a_url():
    scope = project_scope_id(CRED_URL)
    assert scope.startswith(SCOPE_PREFIX)
    digest = scope[len(SCOPE_PREFIX):]
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    # And it is the digest of the credential-FREE form — the credential was
    # gone before hashing, not hidden inside the input of a reversible step.
    assert digest == hashlib.sha256(b"example.com/team/repo").hexdigest()


# ── resolution from a real checkout ───────────────────────────────────────── #

def test_resolution_reads_the_origin_remote(tmp_path):
    repo = _git_repo(tmp_path / "checkout", CLEAN_URL)
    assert resolve_project_scope(repo) == project_scope_id(CLEAN_URL)


def test_a_repo_with_no_remote_has_no_scope(tmp_path):
    assert resolve_project_scope(_git_repo(tmp_path / "local")) is None


def test_a_missing_path_or_non_repo_has_no_scope(tmp_path):
    assert resolve_project_scope(str(tmp_path / "nowhere")) is None
    assert resolve_project_scope("") is None
    assert resolve_project_scope(None) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert resolve_project_scope(str(plain)) is None


def test_repo_root_finds_the_checkout_from_a_subdirectory(tmp_path):
    repo = _git_repo(tmp_path / "checkout", CLEAN_URL)
    sub = tmp_path / "checkout" / "src" / "pkg"
    sub.mkdir(parents=True)
    root = repo_root(str(sub))
    assert root is not None and repo_root(repo) == root
    assert repo_root("") is None and repo_root(None) is None


# ── the credential never touches the database ─────────────────────────────── #

@pytest.mark.asyncio
async def test_a_credential_bearing_remote_is_never_stored_raw(tmp_path):
    """The whole database file, byte for byte: after proposing a lesson from a
    checkout whose origin carries credentials, neither the credential nor any
    URL-shaped remnant of the remote exists anywhere in what was persisted —
    the raw URL cannot be recovered from the stored scope, which is a digest
    of the credential-free form."""
    repo = _git_repo(tmp_path / "checkout", CRED_URL)
    db_path = tmp_path / "b4cred.db"
    s = await Store(db_path).connect()
    try:
        t = Task.new("Pin the images", repo_path=repo)
        await s.create_task(t)
        mem_id = await LearningQueue(s).propose_from_review(
            t, findings=[{"label": "image pinning",
                          "evidence": "ci/images.yaml:12 pins `latest`"}],
            attempt=1, review_round=1)
        assert mem_id is not None
        row = (await s.list_memories(confirmed=False))[0]
        assert row["project_scope"] == project_scope_id(CLEAN_URL)
        # WAL must be folded into the main file before reading its bytes.
        await s.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        await s.close()

    blob = db_path.read_bytes()
    for fragment in (b"s3kr1t-token", b"alice:", b"alice@",
                     b"example.com/team/repo", b"https://example.com"):
        assert fragment not in blob, fragment
    # Control for the instrument: values that WERE stored are findable, so an
    # empty match above is the absence of the secret, not a broken probe.
    assert row["project_scope"].encode() in blob
    assert repo.encode() in blob  # the checkout PATH is stored; paths carry no secret


# ── recall: current project across checkouts, plus globals, nothing else ──── #

@pytest.mark.asyncio
async def test_recall_unifies_two_checkouts_and_excludes_other_projects(store, tmp_path):
    checkout_a = _git_repo(tmp_path / "a", "https://example.com/team/repo.git")
    checkout_b = _git_repo(tmp_path / "b", "https://example.com/team/repo")
    other = _git_repo(tmp_path / "other", "https://example.com/team/unrelated.git")

    async def _add(title, project):
        assert await store.add_memory(
            mem_type="rule", title=title, content=title, project=project,
            confirmed=True, project_scope=await _project_scope_of(project),
        ) is not None

    await _add("lesson from checkout A", checkout_a)
    await _add("lesson from the other project", other)
    assert await store.add_memory(
        mem_type="rule", title="global lesson", content="applies everywhere",
        confirmed=True) is not None

    # Recall FROM CHECKOUT B — a different path, the same repository (the
    # remotes differ only by `.git`).
    got = {m["title"] for m in await store.list_memories(
        confirmed=True, project=checkout_b,
        scope=await _project_scope_of(checkout_b))}
    assert got == {"lesson from checkout A", "global lesson"}


@pytest.mark.asyncio
async def test_recall_without_globals_is_scope_only(store, tmp_path):
    checkout = _git_repo(tmp_path / "a", CLEAN_URL)
    await store.add_memory(
        mem_type="rule", title="global lesson", content="x", confirmed=True)
    await store.add_memory(
        mem_type="rule", title="scoped lesson", content="x", confirmed=True,
        project=checkout, project_scope=await _project_scope_of(checkout))
    got = {m["title"] for m in await store.list_memories(
        confirmed=True, project=checkout,
        scope=await _project_scope_of(checkout), include_global=False)}
    assert got == {"scoped lesson"}


@pytest.mark.asyncio
async def test_a_legacy_path_keyed_row_still_loads_and_gets_stamped(store, tmp_path):
    """Rows written before B4 carry only the path. They keep matching by path;
    `stamp_project_scope` — run when the repo is next actually seen, the only
    moment the path→remote mapping is knowable — upgrades them, after which
    they surface from ANY checkout of the repo."""
    checkout_a = _git_repo(tmp_path / "a", CLEAN_URL)
    checkout_b = _git_repo(tmp_path / "b", CLEAN_URL)
    await store.add_memory(  # a pre-B4 row: no scope
        mem_type="rule", title="legacy lesson", content="x", confirmed=True,
        project=checkout_a)

    scope = resolve_project_scope(checkout_a)
    # Before stamping: visible from its own checkout…
    assert [m["title"] for m in await store.list_memories(
        confirmed=True, project=checkout_a, scope=scope)] == ["legacy lesson"]
    # …but not yet from the other one (legacy rows are path-keyed).
    assert await store.list_memories(
        confirmed=True, project=checkout_b, scope=scope) == []

    assert await store.stamp_project_scope(checkout_a, scope) == 1
    assert [m["title"] for m in await store.list_memories(
        confirmed=True, project=checkout_b, scope=scope)] == ["legacy lesson"]
    # Idempotent: nothing left to stamp.
    assert await store.stamp_project_scope(checkout_a, scope) == 0


@pytest.mark.asyncio
async def test_a_remoteless_repo_keeps_path_scoping(store, tmp_path):
    """No remote → no portable identity → exactly the pre-B4 behaviour:
    keyed and recalled by path, invisible elsewhere."""
    local = _git_repo(tmp_path / "local")
    t = Task.new("fix the thing", repo_path=local)
    await store.create_task(t)
    assert await LearningQueue(store).propose_from_review(
        t, findings=[{"label": "naming", "evidence": "x.py:1"}],
        attempt=1, review_round=1) is not None
    row = (await store.list_memories(confirmed=False))[0]
    assert row["project_scope"] is None
    assert row["project"] == local
    assert [m["title"] for m in await store.list_memories(
        confirmed=False, project=local, scope=None)] == [row["title"]]


# ── the proposal paths write the scope ────────────────────────────────────── #

@pytest.mark.asyncio
async def test_supervisor_harvest_scopes_by_remote_identity(store, tmp_path):
    from no_human.learning.corrections import CorrectionRecord, cluster_corrections

    checkout = _git_repo(tmp_path / "a", CLEAN_URL)
    msg = ("Run the suite with the venv interpreter before declaring "
           "anything done — the bare python on PATH is an older build.")
    clusters = cluster_corrections([
        CorrectionRecord(task_id="t1", project=checkout, message=msg, ts=1.0),
        CorrectionRecord(task_id="t2", project=checkout, message=msg, ts=2.0),
    ])
    assert await LearningQueue(store).propose_from_corrections(
        clusters[0]) is not None
    row = (await store.list_memories(confirmed=False))[0]
    assert row["project_scope"] == project_scope_id(CLEAN_URL)
    assert row["project"] == checkout
