"""Onboarding wizard API tests — real wiring, no stubs.

Exercises the /api/onboarding/* endpoints against a real Store + tmp config,
verifying: status round-trip, bounded repo detection, derive+persist of an
unproven profile, graceful IDE-absent history handling, learning-queue confirm,
and config.yaml persistence on complete.
"""
from __future__ import annotations

import os
import types
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human.api.app import app
from no_human.core.db import Store
from no_human.learning import LearningQueue, TYPE_RULE


@pytest_asyncio.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "test.db").connect()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    app.state.store = store
    # Lightweight config stand-in; persist target redirected off the real ~/.no_human.
    app.state.config = types.SimpleNamespace(
        data={"git": {"github_hosts": ["github.com", "code.example.com"]}}
    )
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


@pytest.mark.asyncio
async def test_status_initially_incomplete(client):
    r = await client.get("/api/onboarding/status")
    assert r.status_code == 200
    assert r.json()["completed"] is False


@pytest.mark.asyncio
async def test_discover_root_param_bounded(client, tmp_path, monkeypatch):
    """The typed-a-folder path: ``?root=`` scans ONLY that folder and still
    obeys ``max_results``. Replaces the retired POST /repos/detect scan."""
    home = tmp_path / "home"
    for i in range(6):
        _seed_repo(home / "scan" / f"r{i}")
    _seed_repo(home / "other" / "elsewhere")   # a repo under a DIFFERENT root
    monkeypatch.setenv("HOME", str(home))

    r = await client.get("/api/repos/discover",
                         params={"root": str(home / "scan"), "limit": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["repos"]) == 2
    assert body["capped"] is True and body["total_found"] == 6
    # Only the named root is scanned — the repo under home/other is never seen.
    assert body["roots"] == [str(home / "scan")]


@pytest.mark.asyncio
async def test_discover_root_outside_home_is_scanned_not_refused(client, tmp_path, monkeypatch):
    """A ``?root=`` outside the home this endpoint is bound to is a user-TYPED
    folder ("Search another folder") and is scanned wherever it resolves — no
    wider a surface than ``GET /api/fs/suggest`` already exposes. Only
    ``extra_scan_roots`` (operator config, not user-typed) stays home-contained;
    see ``test_discover_honours_operator_configured_extra_roots`` for that."""
    home = tmp_path / "home"
    home.mkdir()
    _seed_repo(tmp_path / "outside" / "secret")
    monkeypatch.setenv("HOME", str(home))

    r = await client.get("/api/repos/discover",
                         params={"root": str(tmp_path / "outside")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {x["name"] for x in body["repos"]} == {"secret"}
    assert body["refused"] == [] and body["refusals"] == []


@pytest.mark.asyncio
async def test_detect_route_is_gone(client, tmp_path):
    """The second, unbounded, alphabetical scanner is retired: the POST handler
    no longer exists, so the request falls through to the SPA catch-all
    (``GET /{path:path}``) and answers 405 Method Not Allowed — the same "gone"
    signal the approve-gate paths rely on (app.py, "reach the SPA catch-all
    ... and answer 405"). It is emphatically NOT a 200 with a repo list.
    """
    r = await client.post("/api/onboarding/repos/detect", json={"root": str(tmp_path)})
    assert r.status_code == 405


@pytest.mark.asyncio
async def test_suggest_never_stats_git_under_home(client, tmp_path, monkeypatch):
    """The macOS-TCC fix: /fs/suggest used to stat ``<dir>/.git`` on every
    listed folder to badge it "git repo", and doing that inside Documents/
    Desktop is what raised the "wants to access" prompt DURING setup. The
    rewrite lists names via iterdir only — no ``.git`` stat at all — and never
    lists the protected dirs when the base is home.
    """
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "myrepo" / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    seen: list[str] = []
    real = Path.exists
    def spy(self):
        seen.append(str(self))
        return real(self)
    monkeypatch.setattr(Path, "exists", spy)

    r = await client.get("/api/fs/suggest", params={"path": str(home) + "/"})
    assert r.status_code == 200
    names = {s["name"] for s in r.json()["suggestions"]}
    assert "myrepo" in names
    assert "Documents" not in names, "protected home dirs must not be listed"
    assert not any(s.endswith("/.git") for s in seen), \
        f"suggest must not stat any .git under home: {[s for s in seen if s.endswith('/.git')]}"


@pytest.mark.asyncio
async def test_onboard_repo_persists_unproven_profile(client, store, tmp_path):
    repo = tmp_path / "svc"
    (repo / ".git").mkdir(parents=True)
    repo.joinpath("package.json").write_text('{"scripts":{"test":"jest","lint":"eslint"}}')
    repo.joinpath("package-lock.json").write_text("{}")

    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ecosystem"] == "node"
    assert body["install_cmd"] == "npm ci"
    assert body["proven"] is False
    # Persisted and retrievable, unconfirmed + unproven.
    prof = await store.get_profile(str(repo.resolve()))
    assert prof is not None
    assert prof.confirmed is False
    assert prof.is_usable is False  # not proven → never drives a task


@pytest.mark.asyncio
async def test_reonboard_with_unchanged_commands_keeps_the_proof(client, store, tmp_path):
    """Re-profiling must not destroy verified state (observed 2026-08-17:
    "Profile N repos" wiped proven+confirmed for every already-proven repo).
    Same derived commands ⇒ the proof still describes what would run ⇒ carried."""
    repo = tmp_path / "svc"
    (repo / ".git").mkdir(parents=True)
    repo.joinpath("package.json").write_text('{"scripts":{"test":"jest","lint":"eslint"}}')
    repo.joinpath("package-lock.json").write_text("{}")

    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    prof = await store.get_profile(str(repo.resolve()))
    prof.proven = {"test_cmd": True}
    prof.confirmed = True
    await store.upsert_profile(prof)

    r2 = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r2.status_code == 200, r2.text
    assert r2.json()["proven"] is True
    prof2 = await store.get_profile(str(repo.resolve()))
    assert prof2.proven.get("test_cmd") is True
    assert prof2.confirmed is True
    assert prof2.is_usable is True


@pytest.mark.asyncio
async def test_reonboard_with_changed_test_command_resets_the_proof(client, store, tmp_path):
    """The other direction: a proof attests ONE exact command string, so a
    changed derivation must reset proven+confirmed — carrying it forward would
    let the review gate run a command nobody ever proved."""
    repo = tmp_path / "svc"
    (repo / ".git").mkdir(parents=True)
    repo.joinpath("package.json").write_text('{"scripts":{"test":"jest","lint":"eslint"}}')
    repo.joinpath("package-lock.json").write_text("{}")

    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    prof = await store.get_profile(str(repo.resolve()))
    assert prof.test_cmd  # premise: a test command was derived at all
    prof.proven = {"test_cmd": True}
    prof.confirmed = True
    await store.upsert_profile(prof)

    # Change what derivation RETURNS, not what the script maps to: `npm test`
    # is the derived string whether the script runs jest or vitest, and a
    # same-string re-derivation correctly keeps the proof. Dropping the script
    # and declaring a Makefile target flips the derived command itself.
    repo.joinpath("package.json").write_text('{"scripts":{"lint":"eslint"}}')
    repo.joinpath("Makefile").write_text("test:\n\techo ok\n")
    r2 = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r2.status_code == 200, r2.text
    assert r2.json()["proven"] is False
    prof2 = await store.get_profile(str(repo.resolve()))
    assert prof2.proven == {}
    assert prof2.confirmed is False
    assert prof2.test_cmd != prof.test_cmd  # premise: the derivation really changed


@pytest.mark.asyncio
async def test_reonboard_with_changed_install_keeps_the_test_proof(client, store, tmp_path):
    """FINDING D: a proof is PER-COMMAND. Proving a repo creates a lockfile, so
    re-derivation flips install ``npm install``->``npm ci`` — an unrelated
    change that must NOT wipe the UNCHANGED test proof the review gate runs.
    (The old all-or-nothing carry reset every proof on any command change, so a
    re-onboard silently lost the test command until the user re-proved.)"""
    repo = tmp_path / "svc"
    (repo / ".git").mkdir(parents=True)
    repo.joinpath("package.json").write_text('{"scripts":{"test":"jest","lint":"eslint"}}')
    # no lockfile yet -> install derives as `npm install`
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    prof = await store.get_profile(str(repo.resolve()))
    install_before = prof.install_cmd
    assert install_before == "npm install"
    prof.proven = {"test_cmd": True}
    prof.confirmed = True
    await store.upsert_profile(prof)

    # the prove run creates a lockfile -> re-derive flips install to `npm ci`
    repo.joinpath("package-lock.json").write_text("{}")
    r2 = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r2.status_code == 200, r2.text
    prof2 = await store.get_profile(str(repo.resolve()))
    assert prof2.install_cmd == "npm ci"          # premise: install command really flipped
    assert prof2.test_cmd == prof.test_cmd         # test command unchanged
    assert prof2.proven.get("test_cmd") is True    # the test proof SURVIVED the install flip
    assert r2.json()["proven"] is True
    assert prof2.confirmed is True
    assert prof2.is_usable is True


@pytest.mark.asyncio
async def test_onboard_rejects_non_repo(client, tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(d)})
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.slow  # EH1: >45s of real subprocess work — runs in `run_tests.sh full`/`slow`
async def test_history_extract_returns_valid_shape(client):
    # Environment-agnostic: whether or not a Windsurf IDE is running, the  # term-ok: real IDE names
    # endpoint must return 200 with a well-formed, honest shape (never crash).
    r = await client.post("/api/onboarding/history/extract")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["available"], bool)
    assert isinstance(body["transcripts"], int) and body["transcripts"] >= 0


@pytest.mark.asyncio
async def test_history_analyze_returns_valid_shape(client):
    r = await client.post("/api/onboarding/history/analyze", json={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["proposed"], int) and body["proposed"] >= 0
    # Anything proposed must be queue-visible (source="proposed").
    if body["proposed"]:
        q = LearningQueue(app.state.store)
        pending_ids = {m["id"] for m in await q.pending()}
        assert all(p["id"] in pending_ids for p in body["proposals"])


@pytest.mark.asyncio
async def test_analyze_response_proposals_carry_project(client, monkeypatch):
    """Every proposal in the analyze response names the project it came from,
    so the web step can group them by whether they are in the selected repos.
    The scan is fed a single real correction rooted at a known cwd; the analyzer
    stamps its project from that cwd and the handler surfaces it."""
    import sys
    # `no_human.api.app` the NAME resolves to the FastAPI instance (the package
    # re-exports it), so reach the module object through sys.modules to patch it.
    app_module = sys.modules["no_human.api.app"]
    from no_human.history.extractor import Transcript, Message

    seeded = Transcript(
        cascade_id="cc:seed", title="Fix login", created="2026-07-01",
        cwd="/Users/u/mine",
        messages=[Message(role="user",
                          content="never commit the lockfile", step_type="user")],
    )

    async def fake_gather(days, repo_paths=None):
        return [seeded], {"claude_code": 1}

    monkeypatch.setattr(app_module, "_gather_history", fake_gather)

    r = await client.post("/api/onboarding/history/analyze",
                          json={"days": 30, "repo_paths": ["/Users/u/mine"]})
    assert r.status_code == 200
    proposals = r.json()["proposals"]
    assert proposals, "the seeded correction should produce at least one proposal"
    assert all("project" in p for p in proposals)
    assert any(p["project"] == "/Users/u/mine" for p in proposals)


@pytest.mark.asyncio
async def test_confirm_rules_activates_proposal(client, store):
    # Seed an unconfirmed proposal, then confirm it via the onboarding endpoint.
    mem_id = await store.add_memory(
        mem_type=TYPE_RULE, title="Verify everything",
        content="No assumptions — run it and cite evidence.",
        source="proposed", confirmed=False,
    )
    assert mem_id
    q = LearningQueue(store)
    assert any(m["id"] == mem_id for m in await q.pending())

    r = await client.post("/api/onboarding/rules/confirm", json={"ids": [mem_id]})
    assert r.status_code == 200
    assert r.json()["confirmed"] == 1
    # Now active, no longer pending.
    assert any(m["id"] == mem_id for m in await q.active())
    assert not any(m["id"] == mem_id for m in await q.pending())


@pytest.mark.asyncio
async def test_complete_persists_and_status_reflects(client, tmp_path):
    payload = {"team": "METRICS_CORE", "repos": ["/x/svc"], "docs": ["/docs/adr"]}
    r = await client.post("/api/onboarding/complete", json=payload)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Status now complete, and config.yaml was written to the redirected path.
    s = await client.get("/api/onboarding/status")
    body = s.json()
    assert body["completed"] is True
    assert body["team"] == "METRICS_CORE"
    assert (tmp_path / "config.yaml").exists()


@pytest.mark.asyncio
async def test_reset_clears_only_the_completed_flag(client, tmp_path):
    """A reset is a re-entry, not a wipe.

    `completed` was written True in one place and False nowhere, so the wizard
    removed the only screen that can fix a wrong setup. The fix must clear that
    one flag and nothing else: everything the user is coming back to keep —
    team, repos, docs — stays, in memory AND on disk.
    """
    import yaml

    payload = {"team": "PLATFORM", "repos": ["/x/svc"], "docs": ["/docs/adr"]}
    assert (await client.post("/api/onboarding/complete", json=payload)).status_code == 200

    r = await client.post("/api/onboarding/reset")
    assert r.status_code == 200
    body = r.json()
    assert body["completed"] is False          # the new status comes back
    assert body["team"] == "PLATFORM"          # …with everything else intact
    assert body["repos"] == ["/x/svc"]
    assert body["docs"] == ["/docs/adr"]

    # The wizard renders again…
    s = await client.get("/api/onboarding/status")
    assert s.json()["completed"] is False
    assert s.json()["team"] == "PLATFORM"

    # …and it survives a restart, because it landed on disk too — the only route
    # that existed before this endpoint was hand-editing exactly this file.
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text())["onboarding"]
    assert on_disk["completed"] is False
    assert on_disk["team"] == "PLATFORM"
    assert on_disk["repos"] == ["/x/svc"]
    assert on_disk["docs"] == ["/docs/adr"]


@pytest.mark.asyncio
async def test_complete_records_that_telemetry_was_asked(client, tmp_path):
    """The new one-time consent step marks itself asked via this same call —
    no separate write path; the existing endpoints are reused as-is."""
    import yaml

    payload = {"team": None, "repos": [], "docs": [], "telemetry_asked": True}
    r = await client.post("/api/onboarding/complete", json=payload)
    assert r.status_code == 200
    assert r.json()["onboarding"]["telemetry_asked"] is True

    s = await client.get("/api/onboarding/status")
    assert s.json()["telemetry_asked"] is True

    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text())["onboarding"]
    assert on_disk["telemetry_asked"] is True


@pytest.mark.asyncio
async def test_complete_without_the_flag_is_unchanged(client, tmp_path):
    """An install that already saw the step (or a caller that never sends the
    field at all) must not have `telemetry_asked` fabricated for it."""
    payload = {"team": None, "repos": [], "docs": []}
    r = await client.post("/api/onboarding/complete", json=payload)
    assert r.status_code == 200
    assert "telemetry_asked" not in r.json()["onboarding"]

    s = await client.get("/api/onboarding/status")
    assert not s.json().get("telemetry_asked")


@pytest.mark.asyncio
async def test_the_ask_is_sticky_across_reset_and_re_complete(client, tmp_path):
    """Once asked, never un-asked — a re-run of the wizard (File > Re-run
    Setup…) must not resurrect a question the user already answered."""
    first = {"team": None, "repos": [], "docs": [], "telemetry_asked": True}
    assert (await client.post("/api/onboarding/complete", json=first)).status_code == 200

    r = await client.post("/api/onboarding/reset")
    assert r.status_code == 200
    assert r.json()["telemetry_asked"] is True  # reset clears only `completed`

    s = await client.get("/api/onboarding/status")
    assert s.json()["completed"] is False
    assert s.json()["telemetry_asked"] is True

    # Re-completing the wizard WITHOUT the flag (the field the fresh form would
    # send once shouldAskTelemetry() sees telemetry_asked=True and skips the
    # step) must not lose the sticky bit.
    again = {"team": None, "repos": [], "docs": []}
    r = await client.post("/api/onboarding/complete", json=again)
    assert r.status_code == 200
    assert r.json()["onboarding"]["telemetry_asked"] is True

    s = await client.get("/api/onboarding/status")
    assert s.json()["telemetry_asked"] is True


@pytest.fixture
def tmp_repo(tmp_path):
    return _seed_repo(tmp_path / "myrepo")


@pytest.mark.asyncio
async def test_minimal_complete_creates_project_named_after_repo_and_defers_rest(client, tmp_repo):
    r = await client.post("/api/onboarding/complete", json={"completed": True, "minimal": True, "repo_path": str(tmp_repo)})
    assert r.status_code == 200
    projects = (await client.get("/api/projects")).json()
    assert any(p["name"] == tmp_repo.name and str(tmp_repo) in p["repo_paths"] for p in projects)
    assert (await client.get("/api/onboarding/deferred")).json()["deferred"] == ["docs", "integrations", "history", "rules"]


@pytest.mark.asyncio
async def test_deferred_step_done_removes_it(client, tmp_repo):
    await client.post("/api/onboarding/complete", json={"completed": True, "minimal": True, "repo_path": str(tmp_repo)})
    await client.post("/api/onboarding/deferred/docs/done")
    assert "docs" not in (await client.get("/api/onboarding/deferred")).json()["deferred"]


@pytest.mark.asyncio
async def test_minimal_without_repo_is_400(client):
    assert (await client.post("/api/onboarding/complete", json={"completed": True, "minimal": True})).status_code == 400


# --------------------------------------------------------------------------- #
# GET /api/repos/discover — the typed-path replacement. The endpoint is bound  #
# to the process's home, so these tests relocate HOME onto tmp_path rather     #
# than accepting a caller-supplied root (which would be an unbounded scan      #
# surface on a localhost service).                                             #
# --------------------------------------------------------------------------- #

def _seed_repo(path):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


@pytest.mark.asyncio
async def test_discover_lists_repos_from_the_conventional_roots(client, tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_repo(home / "git" / "svc-a")
    _seed_repo(home / "Projects" / "svc-b")
    monkeypatch.setenv("HOME", str(home))

    r = await client.get("/api/repos/discover")
    assert r.status_code == 200, r.text
    body = r.json()
    names = {x["name"] for x in body["repos"]}
    assert names == {"svc-a", "svc-b"}
    row = next(x for x in body["repos"] if x["name"] == "svc-a")
    assert row["path"] == str(home / "git" / "svc-a")
    assert row["is_git"] is True
    assert row["branch"] == "main"
    assert row["dirty"] is False
    assert body["capped"] is False
    assert isinstance(body["elapsed_ms"], int)


@pytest.mark.asyncio
async def test_discover_honours_operator_configured_extra_roots(client, tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_repo(home / "work" / "acme")
    monkeypatch.setenv("HOME", str(home))
    app.state.config.data["onboarding"] = {"extra_scan_roots": ["~/work"]}
    try:
        r = await client.get("/api/repos/discover")
        assert {x["name"] for x in r.json()["repos"]} == {"acme"}
    finally:
        app.state.config.data.pop("onboarding", None)


@pytest.mark.asyncio
async def test_discover_caps_and_says_so_rather_than_truncating_silently(client, tmp_path, monkeypatch):
    home = tmp_path / "home"
    for i in range(6):
        _seed_repo(home / "git" / f"r{i}")
    monkeypatch.setenv("HOME", str(home))

    r = await client.get("/api/repos/discover", params={"limit": 2})
    body = r.json()
    assert len(body["repos"]) == 2
    assert body["capped"] is True
    assert body["total_found"] == 6
    assert body["note"]


@pytest.mark.asyncio
async def test_discover_clamps_an_absurd_limit(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    r = await client.get("/api/repos/discover", params={"limit": 10 ** 6})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_discover_on_a_bare_home_is_an_empty_list_not_an_error(client, tmp_path, monkeypatch):
    home = tmp_path / "empty-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    r = await client.get("/api/repos/discover")
    assert r.status_code == 200
    assert r.json()["repos"] == []
    assert r.json()["roots_scanned"] == []


@pytest.mark.asyncio
async def test_discover_survives_an_unreadable_directory_instead_of_500ing(
    client, tmp_path, monkeypatch
):
    """One ``chmod 000`` folder anywhere under a scan root used to raise
    ``PermissionError`` straight out of the walk. This endpoint has no
    exception handler in front of it, so that was an unhandled 500 on the
    FIRST-RUN onboarding path, and it took the healthy repos down with it.
    """
    home = tmp_path / "home"
    _seed_repo(home / "Projects" / "healthy")
    locked = home / "Projects" / "locked"
    (locked / ".git").mkdir(parents=True)
    locked.chmod(0o000)
    monkeypatch.setenv("HOME", str(home))
    try:
        # Prove the scenario is genuinely hostile before trusting the result.
        # (Under root the bits mean nothing; the assertions below still hold,
        # they just prove less there. A skip marker would cost a tamper signal
        # for no gain.)
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            with pytest.raises(PermissionError):
                (locked / ".git").exists()
        r = await client.get("/api/repos/discover")
    finally:
        locked.chmod(0o755)

    assert r.status_code == 200, r.text
    assert {x["name"] for x in r.json()["repos"]} == {"healthy"}


@pytest.mark.asyncio
async def test_discover_refuses_a_conventional_root_that_leaves_home(
    client, tmp_path, monkeypatch
):
    """``~/Dev -> /Volumes/BigDisk`` is an ordinary setup, and it walked the
    scan straight out of the home directory this endpoint is bound to."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    _seed_repo(outside / "secretrepo")
    (home / "Dev").symlink_to(outside)
    monkeypatch.setenv("HOME", str(home))

    r = await client.get("/api/repos/discover")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repos"] == []
    assert body["roots_refused"], "a refused root must be reported, not silently dropped"


async def test_docs_generate_returns_202_and_job_is_pollable(client, tmp_path):
    repo = tmp_path / "wikirepo"
    repo.mkdir()
    r = await client.post("/api/onboarding/docs/generate", json={"repo_path": str(repo)})
    assert r.status_code == 202 and "job_id" in r.json()
    j = await client.get(f"/api/onboarding/docs/jobs/{r.json()['job_id']}")
    assert j.status_code == 200 and j.json()["status"] in {"queued", "running", "done", "failed"}


async def test_docs_detect_lists_existing_docs(client, tmp_path):
    repo = tmp_path / "detectrepo"
    (repo / "docs").mkdir(parents=True)
    (repo / "README.md").write_text("# hi")
    r = await client.get("/api/onboarding/docs/detect", params={"repo": str(repo)})
    assert r.status_code == 200
    found = r.json()["found"]
    assert "README.md" in found and "docs" in found and "CONTRIBUTING.md" not in found


async def test_docs_job_missing_is_404(client):
    r = await client.get("/api/onboarding/docs/jobs/nope")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_gather_history_scopes_ide_transcripts_to_selected_repos(monkeypatch):
    """Feedback I4: the IDE (Windsurf) extractor is machine-wide and used to
    pull conversations from EVERY workspace even when onboarding was focused on
    one repo. _gather_history must drop IDE transcripts whose workspace is
    outside the selected repos, keep those inside, and drop unscopable ones
    (no derivable project). The CC source is patched to empty to isolate."""
    import sys
    # `no_human.api.app` the NAME is the FastAPI instance; reach the module.
    app_module = sys.modules["no_human.api.app"]
    from no_human.history.extractor import Transcript, Message

    inside = Transcript(cascade_id="w:in", title="in", created="2026-07-01",
                        workspaces=["file:///Users/u/mine"],
                        messages=[Message(role="user", content="x", step_type="user")])
    outside = Transcript(cascade_id="w:out", title="out", created="2026-07-01",
                        workspaces=["file:///Users/u/other"],
                        messages=[Message(role="user", content="y", step_type="user")])
    sibling = Transcript(cascade_id="w:sib", title="sib", created="2026-07-01",
                        workspaces=["file:///Users/u/mine-two"],  # sibling, NOT under mine
                        messages=[Message(role="user", content="z", step_type="user")])
    noproject = Transcript(cascade_id="w:none", title="none", created="2026-07-01",
                        workspaces=[],  # unscopable
                        messages=[Message(role="user", content="q", step_type="user")])

    def fake_extract(*, days, client=None):
        return [inside, outside, sibling, noproject]

    monkeypatch.setattr("no_human.history.extractor.extract_transcripts", fake_extract)
    monkeypatch.setattr(
        "no_human.history.claude_code.extract_claude_code_transcripts",
        lambda *, days, repo_paths=None: [])

    transcripts, sources = await app_module._gather_history(
        30, repo_paths=["/Users/u/mine"])
    ids = {t.cascade_id for t in transcripts}
    assert ids == {"w:in"}, f"only the in-repo IDE transcript should survive, got {ids}"
    assert sources["windsurf"] == 1

    # Positive control: no repo scope keeps every IDE transcript (unchanged CLI path).
    all_transcripts, all_sources = await app_module._gather_history(30, repo_paths=None)
    assert {t.cascade_id for t in all_transcripts} == {"w:in", "w:out", "w:sib", "w:none"}
    assert all_sources["windsurf"] == 4
