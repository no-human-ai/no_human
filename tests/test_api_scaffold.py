"""POST /api/repos/scaffold — create a brand-new repo from the GUI.

The composer's "create a new repo" affordance calls this: given a parent
directory and a name it creates the directory, `git init`s it, writes a
minimal README, makes the initial commit under the AGENT identity (the history
must say plainly which commits a machine wrote), and registers the result as a
project so the composer can proceed in it.

The parent path is operator input flowing straight into filesystem writes, so
the validation tests here are the security surface: resolve() before the
$HOME containment check, and a name regex that a `..` cannot pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app


@pytest_asyncio.fixture
async def client(store_factory, tmp_path, monkeypatch):
    """Same shape as test_auth_api.py's client, plus a redirected $HOME so the
    under-home containment check can be exercised against tmp_path."""
    from no_human.config import load_config
    store = await store_factory("test.db")
    app.state.store = store
    app.state.config = load_config(tmp_path / "config.yaml")
    fake_home = (tmp_path / "home").resolve()
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    transport = ASGITransport(app=app)
    # A real browser always sends Origin; the endpoint mutates the filesystem,
    # so like the credential routes it refuses a cross-origin caller.
    async with AsyncClient(transport=transport, base_url="http://localhost",
                           headers={"Origin": "http://127.0.0.1:8420"}) as c:
        yield c


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


# ------------------------------- happy path -------------------------------- #

@pytest.mark.asyncio
async def test_scaffold_creates_repo_with_one_commit_and_readme(client):
    home = Path.home()
    parent = home / "git"
    parent.mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(parent), "name": "shiny"})
    assert r.status_code == 201, r.text
    body = r.json()
    target = Path(body["repo_path"])
    assert target == parent / "shiny"
    assert target.is_dir()
    assert (target / ".git").is_dir()
    # Minimal README with the repo's name as the title.
    assert (target / "README.md").read_text() == "# shiny\n"
    # Exactly one commit, and the README is tracked (not just on disk).
    assert _git(target, "rev-list", "--count", "HEAD") == "1"
    assert "README.md" in _git(target, "ls-files")
    # The worktree is clean - nothing half-staged.
    assert _git(target, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_scaffold_commits_under_the_agent_identity(client):
    """A machine-made commit carries the agent identity, both as author and
    committer - never the operator's global git config."""
    home = Path.home()
    (home / "git").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "git"), "name": "ident"})
    assert r.status_code == 201, r.text
    target = Path(r.json()["repo_path"])
    who = _git(target, "log", "-1", "--format=%an|%ae|%cn|%ce")
    # config.yaml in the fixture is empty, so the DEFAULT_CONFIG git identity
    # applies (config.py git.agent_identity_*).
    assert who == "no_human|no-human@acme.com|no_human|no-human@acme.com"


@pytest.mark.asyncio
async def test_scaffold_registers_a_project(client):
    home = Path.home()
    (home / "git").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "git"), "name": "proj-reg"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["project_id"]
    pr = await client.get(f"/api/projects/{body['project_id']}")
    assert pr.status_code == 200
    proj = pr.json()
    assert proj["name"] == "proj-reg"
    assert proj["repo_paths"] == [body["repo_path"]]
    assert proj["primary_repo"] == body["repo_path"]


@pytest.mark.asyncio
async def test_scaffolded_repo_passes_create_project_validation(client):
    """The repo the endpoint made must be a repo POST /api/projects would
    accept - is_dir plus a .git - or the composer's next step breaks."""
    home = Path.home()
    (home / "git").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "git"), "name": "valid"})
    assert r.status_code == 201
    r2 = await client.post("/api/projects", json={
        "name": "valid-again", "repo_paths": [r.json()["repo_path"]]})
    assert r2.status_code == 201, r2.text


# ------------------------------- validation -------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_name", [
    "", "a b", "a/b", "../evil", "..", ".", "a" * 81, "name\n", "café",
])
async def test_scaffold_rejects_bad_names(client, bad_name):
    parent = Path.home() / "git"
    parent.mkdir(exist_ok=True)
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(parent), "name": bad_name})
    assert r.status_code == 400, f"{bad_name!r} -> {r.status_code}"
    assert "name" in r.json()["detail"]


@pytest.mark.asyncio
async def test_scaffold_rejects_relative_parent(client):
    r = await client.post("/api/repos/scaffold",
                          json={"parent": "git/somewhere", "name": "x"})
    assert r.status_code == 400
    assert "absolute" in r.json()["detail"]


@pytest.mark.asyncio
async def test_scaffold_rejects_parent_outside_home(client, tmp_path):
    outside = tmp_path / "outside-home"
    outside.mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(outside), "name": "x"})
    assert r.status_code == 400
    assert "home" in r.json()["detail"]
    assert not (outside / "x").exists()


@pytest.mark.asyncio
async def test_scaffold_rejects_dotdot_traversal_out_of_home(client, tmp_path):
    """resolve() must run BEFORE the containment check: a parent that
    lexically starts under $HOME but ..-escapes it is outside $HOME."""
    (tmp_path / "elsewhere").mkdir()
    sneaky = str(Path.home() / "git" / ".." / ".." / "elsewhere")
    (Path.home() / "git").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": sneaky, "name": "x"})
    assert r.status_code == 400
    assert "home" in r.json()["detail"]
    assert not (tmp_path / "elsewhere" / "x").exists()


@pytest.mark.asyncio
async def test_scaffold_rejects_missing_parent(client):
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(Path.home() / "nope"), "name": "x"})
    assert r.status_code == 400
    assert "directory" in r.json()["detail"]


@pytest.mark.asyncio
async def test_scaffold_rejects_file_parent(client):
    f = Path.home() / "afile"
    f.write_text("not a dir")
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(f), "name": "x"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_scaffold_409_when_target_exists(client):
    parent = Path.home() / "git"
    parent.mkdir()
    (parent / "taken").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(parent), "name": "taken"})
    assert r.status_code == 409
    assert "exists" in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dotgit", [".git", ".GIT"])
async def test_scaffold_rejects_dot_git_name(client, dotgit):
    """".git" (any case - the operator's filesystem is case-insensitive) is
    git's own metadata directory, not a repo name. The detail must say THIS
    check failed, not the generic charset message."""
    parent = Path.home() / "git"
    parent.mkdir(exist_ok=True)
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(parent), "name": dotgit})
    assert r.status_code == 400, f"{dotgit!r} -> {r.status_code}"
    detail = r.json()["detail"]
    assert "name" in detail
    assert ".git" in detail  # says WHICH check refused it
    assert not (parent / dotgit).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("dots", ["...", "...."])
async def test_scaffold_rejects_dots_only_names(client, dots):
    """Dots-only names match the charset regex but are path navigation, like
    the "."/".." already refused. The detail must say THIS check failed."""
    parent = Path.home() / "git"
    parent.mkdir(exist_ok=True)
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(parent), "name": dots})
    assert r.status_code == 400, f"{dots!r} -> {r.status_code}"
    detail = r.json()["detail"]
    assert "name" in detail
    assert "dots" in detail  # says WHICH check refused it
    assert not (parent / dots).exists()


# --------------------------- error-path cleanup ----------------------------- #

@pytest.mark.asyncio
async def test_scaffold_git_failure_removes_the_half_made_dir(client, monkeypatch):
    """Forced git failure after mkdir: the endpoint DID create the dir, so the
    error path must remove it or a retry is an instant 409 on our own debris.
    Mutation target: deleting the rmtree in the error path must fail this."""
    parent = Path.home() / "git"
    parent.mkdir()

    def _boom(argv, *args, **kwargs):
        raise subprocess.CalledProcessError(128, argv, stderr="forced failure")

    # app.py does `import subprocess` at module level, so its _git() calls
    # THIS shared module object's `run`; undone by monkeypatch after the test.
    monkeypatch.setattr(subprocess, "run", _boom)
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(parent), "name": "halfmade"})
    assert r.status_code == 500
    assert not (parent / "halfmade").exists()


@pytest.mark.asyncio
async def test_scaffold_mkdir_race_does_not_delete_the_other_writers_dir(
        client, monkeypatch):
    """TOCTOU: target.exists() saw nothing, but another writer creates target
    before our mkdir runs, so mkdir raises FileExistsError. The endpoint did
    NOT create that dir - the error path must NOT rmtree the other writer's
    files."""
    parent = Path.home() / "git"
    parent.mkdir()
    target = parent / "contested"
    real_mkdir = Path.mkdir

    def racing_mkdir(self, *args, **kwargs):
        if self.name == "contested":
            # The other writer wins the race after the exists() check: their
            # dir (with content) appears, then our mkdir raises - exactly what
            # the OS reports in a real race.
            real_mkdir(self)
            (self / "their-file.txt").write_text("not ours")
            raise FileExistsError(17, "File exists", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(parent), "name": "contested"})
    assert r.status_code == 500
    # The other writer's dir and content SURVIVE - we never created them.
    assert target.is_dir()
    assert (target / "their-file.txt").read_text() == "not ours"


# ------------------------------ env hygiene --------------------------------- #

@pytest.mark.asyncio
async def test_scaffold_ignores_git_author_env(client, monkeypatch):
    """GIT_AUTHOR_NAME/EMAIL in the server's environment override `-c user.*`
    in git's precedence order, so they must be scrubbed from the child env -
    the commit stays under the agent identity."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Operator Env")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "operator@env.example")
    home = Path.home()
    (home / "git").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "git"), "name": "envproof"})
    assert r.status_code == 201, r.text
    target = Path(r.json()["repo_path"])
    who = _git(target, "log", "-1", "--format=%an|%ae|%cn|%ce")
    assert who == "no_human|no-human@acme.com|no_human|no-human@acme.com"


@pytest.mark.asyncio
async def test_scaffold_ignores_git_dir_env(client, monkeypatch, tmp_path):
    """A GIT_DIR pointing outside home must not redirect any git write: the
    repo lands in target/.git, nothing appears at GIT_DIR, and the endpoint
    still succeeds."""
    decoy = tmp_path / "gitdir-decoy"
    monkeypatch.setenv("GIT_DIR", str(decoy))
    home = Path.home()
    (home / "git").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "git"), "name": "gitdirproof"})
    assert r.status_code == 201, r.text
    assert not decoy.exists()
    target = Path(r.json()["repo_path"])
    assert (target / ".git").is_dir()
    # Clear it before reading, or the helper below is redirected too.
    monkeypatch.delenv("GIT_DIR")
    assert _git(target, "rev-list", "--count", "HEAD") == "1"
    assert not decoy.exists()


@pytest.mark.asyncio
async def test_scaffold_ignores_git_object_directory_env(
        client, monkeypatch, tmp_path):
    """GIT_OBJECT_DIRECTORY redirects where git writes loose objects. Pointed
    outside home it silently drains the new repo's objects there while the
    endpoint still answers 201 - the child env must not carry it."""
    decoy = tmp_path / "objdir-decoy"
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(decoy))
    home = Path.home()
    (home / "git").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "git"), "name": "objdirproof"})
    assert r.status_code == 201, r.text
    assert not decoy.exists()
    target = Path(r.json()["repo_path"])
    monkeypatch.delenv("GIT_OBJECT_DIRECTORY")
    # The objects are in the repo, not the decoy: a commit that can be read
    # back with the decoy gone proves nothing was written outside home.
    assert _git(target, "rev-list", "--count", "HEAD") == "1"
    assert _git(target, "cat-file", "-p", "HEAD:README.md") == "# objdirproof"
    assert not decoy.exists()


@pytest.mark.asyncio
async def test_scaffold_ignores_git_common_dir_env(client, monkeypatch, tmp_path):
    """GIT_COMMON_DIR relocates the shared part of the git dir (refs, config,
    objects). Pointed outside home it makes git materialise that directory
    there and the scaffold fails - the child env must not carry it."""
    decoy = tmp_path / "commondir-decoy"
    monkeypatch.setenv("GIT_COMMON_DIR", str(decoy))
    home = Path.home()
    (home / "git").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "git"), "name": "commonproof"})
    assert r.status_code == 201, r.text
    assert not decoy.exists()
    target = Path(r.json()["repo_path"])
    assert (target / ".git").is_dir()
    monkeypatch.delenv("GIT_COMMON_DIR")
    assert _git(target, "rev-list", "--count", "HEAD") == "1"
    assert not decoy.exists()


@pytest.mark.asyncio
async def test_scaffold_child_env_carries_only_the_allowlist(client, monkeypatch):
    """Any GIT_* the operator's shell exports is a potential redirect, so the
    child env is built from an allowlist rather than by subtracting known-bad
    names. A GIT_* nobody enumerated must not reach the child."""
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/nope/alt")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/nope/ceiling")
    seen: dict[str, str] = {}
    real_run = subprocess.run

    def _capture(argv, *args, **kwargs):
        seen.update(kwargs.get("env") or {})
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _capture)
    home = Path.home()
    (home / "git").mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "git"), "name": "allowlist"})
    assert r.status_code == 201, r.text
    leaked = {k for k in seen if k.startswith("GIT_")} - {"GIT_CONFIG_NOSYSTEM"}
    assert leaked == set(), f"unexpected GIT_* reached the child: {sorted(leaked)}"


# ---------------------------- project-name clash ---------------------------- #

@pytest.mark.asyncio
async def test_scaffold_name_clash_409s_before_touching_the_disk(client):
    """Two parents, one name: ~/a/dup registers project 'dup', so ~/b/dup
    cannot. The clash must be refused BEFORE anything is created, or the
    second directory exists on disk with no project - unregisterable through
    this endpoint (a retry 409s on the path check) and the operator is stuck."""
    home = Path.home()
    (home / "a").mkdir()
    (home / "b").mkdir()
    first = await client.post("/api/repos/scaffold",
                              json={"parent": str(home / "a"), "name": "dup"})
    assert first.status_code == 201, first.text

    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "b"), "name": "dup"})
    assert r.status_code == 409, r.text
    # No orphan: nothing was created under the second parent.
    assert not (home / "b" / "dup").exists()
    assert list((home / "b").iterdir()) == []
    # The message says what to do about it, not just that it happened.
    detail = r.json()["detail"]
    assert "dup" in detail
    assert "different name" in detail
    # The first repo is untouched.
    assert (home / "a" / "dup" / ".git").is_dir()


@pytest.mark.asyncio
async def test_scaffold_name_clash_race_leaves_no_orphan(client, monkeypatch):
    """The pre-check cannot close the window entirely: another writer can
    register the name between the check and our INSERT. That path still 409s,
    and must still leave no directory behind."""
    home = Path.home()
    (home / "b").mkdir()
    store = app.state.store
    real_create = store.create_project

    async def _clash(project):
        raise RuntimeError("UNIQUE constraint failed: projects.name")

    monkeypatch.setattr(store, "create_project", _clash)
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(home / "b"), "name": "raced"})
    assert real_create is not None
    assert r.status_code == 409, r.text
    assert not (home / "b" / "raced").exists()


@pytest.mark.asyncio
async def test_scaffold_refuses_cross_origin(client):
    """Filesystem-mutating write: same posture as the credential routes - a
    drive-by page on another origin must not be able to litter $HOME."""
    parent = Path.home() / "git"
    parent.mkdir()
    r = await client.post("/api/repos/scaffold",
                          json={"parent": str(parent), "name": "driveby"},
                          headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert not (parent / "driveby").exists()
