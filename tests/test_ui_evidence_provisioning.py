"""no-human-67 follow-up: no product flow ever configured
``ProjectProfile.ui_evidence`` for a customer's repo, so the visual-proof-walks
feature was unreachable even with Playwright installed. This exercises the
provisioning pipeline added to close that gap: detect (extends the existing
node ecosystem detector) -> one-confirm-offer -> dual write (project.yml + DB
row), plus its surfacing in `nh doctor` and the wizard's
`/api/onboarding/repos/ui-evidence` endpoint.

Real repos on disk, a real Store, real file I/O throughout — no stubs for the
things under test, per the intake's "realistic fixtures" answer.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from no_human.api.app import app
from no_human.core.db import Store
from no_human.doctor import diagnose, ui_evidence_rows
from no_human.onboard import (
    DeclarationDeriver,
    ProjectYmlPersistError,
    apply_ui_evidence_suggestion,
    detect_dev_server,
    offer_ui_evidence,
    persist_profile,
    ui_evidence_configured,
    ui_evidence_suggestion,
)
from no_human.profile import PROFILE_RELPATH, ProjectProfile

# --------------------------------------------------------------------------- #
# fixtures: two realistic repos — one WITH a `dev` script, one WITHOUT         #
# --------------------------------------------------------------------------- #


def _vite_repo(root):
    """A realistic Vite-based frontend: `npm run dev` + `vite` as a
    devDependency — the exact shape `detect_dev_server` is documented to key
    off (script name + a known framework in deps/devDeps)."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({
        "name": "web", "version": "0.0.0",
        "scripts": {"dev": "vite", "build": "vite build", "test": "vitest run"},
        "devDependencies": {"vite": "^5.0.0"},
    }))
    return root


def _plain_node_repo(root):
    """A realistic backend service repo: real package.json, real scripts, but
    NO `dev` script at all — the negative fixture."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({
        "name": "svc", "version": "0.0.0",
        "scripts": {"start": "node index.js", "test": "jest"},
        "dependencies": {"express": "^4.18.0"},
    }))
    return root


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    import types

    import no_human.config as nh_config

    app.state.store = store
    app.state.config = types.SimpleNamespace(data={"git": {"github_hosts": ["github.com"]}})
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


# --------------------------------------------------------------------------- #
# detect_dev_server — extends the existing node detector, closed port table   #
# --------------------------------------------------------------------------- #


def test_detect_dev_server_finds_vite_port(tmp_path):
    repo = _vite_repo(tmp_path / "web")
    detected = detect_dev_server(repo)
    assert detected == {
        "start_cmd": "npm run dev",
        "base_url": "http://localhost:5173",
        "port": 5173,
        "framework": "vite",
        "source": "package.json:scripts.dev",
    }


def test_detect_dev_server_none_without_dev_script(tmp_path):
    repo = _plain_node_repo(tmp_path / "svc")
    assert detect_dev_server(repo) is None


def test_declaration_deriver_carries_the_dev_server(tmp_path):
    """`DeclarationDeriver.derive` (the existing node-ecosystem detector's
    entry point, called by both `nh onboard` and the API wizard) must surface
    the SAME fact `detect_dev_server` computes directly — proving this is an
    extension of the one detector, not a second detector living only in
    tests."""
    repo = _vite_repo(tmp_path / "web")
    direct = detect_dev_server(repo)
    derived = DeclarationDeriver().derive(repo)
    assert derived.ecosystem == "node"
    assert derived.dev_server == direct


def test_declaration_deriver_dev_server_none_without_dev_script(tmp_path):
    repo = _plain_node_repo(tmp_path / "svc")
    derived = DeclarationDeriver().derive(repo)
    assert derived.ecosystem == "node"
    assert derived.dev_server is None


def test_detect_dev_server_none_without_known_framework(tmp_path):
    """A `dev` script exists, but no framework from the closed port table is a
    dependency — deliberately no guess, per the rigid-scope assumption."""
    repo = tmp_path / "mystery"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({
        "scripts": {"dev": "node server.js"},
        "dependencies": {"some-unlisted-framework": "1.0.0"},
    }))
    assert detect_dev_server(repo) is None


# --------------------------------------------------------------------------- #
# ui_evidence_suggestion — manual-config-wins, no-dev-script-means-no-suggestion #
# --------------------------------------------------------------------------- #


def test_suggestion_present_when_detected_and_unconfigured(tmp_path):
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    assert ui_evidence_configured(prof) is False
    sug = ui_evidence_suggestion(prof, repo)
    assert sug is not None
    assert sug["start_cmd"] == "npm run dev"
    assert sug["base_url"] == "http://localhost:5173"
    assert sug["port"] == 5173
    assert "not configured" in sug["gap"] or "not configured" in sug["gap"].lower() \
        or "detected" in sug["gap"]
    # execution disclosure (risk parity with test_cmd): the offer must say
    # plainly that accepting grants the harness permission to RUN the
    # command, not just display it — since the dev-server boot lands on
    # every UI-touching attempt.
    assert "will RUN this command" in sug["gap"]
    assert "stop it after" in sug["gap"]


def test_no_suggestion_without_dev_script(tmp_path):
    repo = _plain_node_repo(tmp_path / "svc")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    assert ui_evidence_suggestion(prof, repo) is None


def test_manual_config_wins_never_reprompts(tmp_path):
    """Once ui_evidence is configured — by hand or by a prior accept — the
    suggestion must never fire again, even though the repo still matches the
    detector."""
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    prof.ui_evidence = {**prof.ui_evidence, "enabled": True,
                         "start_cmd": "npm run dev", "base_url": "http://localhost:5173"}
    assert ui_evidence_configured(prof) is True
    assert ui_evidence_suggestion(prof, repo) is None


# --------------------------------------------------------------------------- #
# apply + persist — dual write to BOTH project.yml and the DB row             #
# --------------------------------------------------------------------------- #


async def test_apply_and_persist_writes_both_yml_and_db(store, tmp_path):
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    sug = ui_evidence_suggestion(prof, repo)
    assert sug is not None

    apply_ui_evidence_suggestion(prof, sug)
    await persist_profile(store, prof)

    yml_path = repo / PROFILE_RELPATH
    assert yml_path.exists()
    on_disk = ProjectProfile.load(repo)
    assert on_disk.ui_evidence["enabled"] is True
    assert on_disk.ui_evidence["start_cmd"] == "npm run dev"
    assert on_disk.ui_evidence["base_url"] == "http://localhost:5173"

    from_db = await store.get_profile(str(repo))
    assert from_db.ui_evidence["enabled"] is True
    assert from_db.ui_evidence["start_cmd"] == on_disk.ui_evidence["start_cmd"]
    assert from_db.ui_evidence["base_url"] == on_disk.ui_evidence["base_url"]


async def test_offer_ui_evidence_decline_writes_nothing(store, tmp_path):
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    sug = ui_evidence_suggestion(prof, repo)
    assert sug is not None

    enabled = await offer_ui_evidence(store, prof, sug, ask=lambda _prompt: False)
    assert enabled is False
    assert not (repo / PROFILE_RELPATH).exists()
    assert await store.get_profile(str(repo)) is None


async def test_offer_ui_evidence_accept_writes_both(store, tmp_path):
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    sug = ui_evidence_suggestion(prof, repo)

    seen_prompts = []
    enabled = await offer_ui_evidence(
        store, prof, sug, ask=lambda prompt: seen_prompts.append(prompt) or True
    )
    assert enabled is True
    assert seen_prompts == ["Enable visual-proof walks?"]  # the ONE exact prompt
    from_db = await store.get_profile(str(repo))
    assert from_db.ui_evidence["enabled"] is True


async def test_offer_ui_evidence_yml_failure_is_not_reported_as_success(store, tmp_path, monkeypatch):
    """A `project.yml` write failure (permissions, read-only checkout, missing
    `.no_human` dir) must never be reported as an enabled dual-write: no
    green success signal, and no DB row claiming ``enabled`` while
    project.yml stays untouched — that split-brain state is exactly what the
    AC3 dual-write invariant forbids."""
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    sug = ui_evidence_suggestion(prof, repo)
    assert sug is not None

    def _boom(self):
        raise OSError("read-only file system")

    monkeypatch.setattr(ProjectProfile, "save", _boom)

    with pytest.raises(ProjectYmlPersistError):
        await offer_ui_evidence(store, prof, sug, ask=lambda _prompt: True)

    assert not (repo / PROFILE_RELPATH).exists()
    # No split-brain: the DB write must be skipped too, not silently applied.
    assert await store.get_profile(str(repo)) is None


async def test_persist_profile_yml_failure_skips_the_db_write(store, tmp_path, monkeypatch):
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    sug = ui_evidence_suggestion(prof, repo)
    apply_ui_evidence_suggestion(prof, sug)

    monkeypatch.setattr(ProjectProfile, "save", lambda self: (_ for _ in ()).throw(OSError("nope")))

    with pytest.raises(ProjectYmlPersistError):
        await persist_profile(store, prof)

    assert await store.get_profile(str(repo)) is None


# --------------------------------------------------------------------------- #
# doctor — surfaces both current state and, side-by-side, the suggested state #
# --------------------------------------------------------------------------- #


async def test_ui_evidence_rows_no_dev_script_no_suggestion(store, tmp_path):
    repo = _plain_node_repo(tmp_path / "svc")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    await store.upsert_profile(prof)

    rows = ui_evidence_rows(await store.list_profiles())
    assert len(rows) == 1
    assert rows[0]["configured"] is False
    assert rows[0]["suggestion"] is None


async def test_diagnose_advisory_names_the_gap(store, tmp_path):
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    await store.upsert_profile(prof)

    d = await diagnose(store)
    assert d.healthy, "an advisory must never affect the healthy predicate"
    assert any("VISUAL-PROOF WALKS NOT CONFIGURED" in a for a in d.advisories)
    assert any("npm run dev" in a and ":5173" in a for a in d.advisories)
    # execution disclosure (risk parity with test_cmd): the advisory that
    # tells a human to enable this must say plainly the harness will RUN the
    # command, same as the CLI offer's gap text does.
    assert any("will RUN this command" in a and "stop it after" in a
               for a in d.advisories)
    # side-by-side: the row carries BOTH current (unconfigured) and suggested state
    row = next(r for r in d.ui_evidence if r["repo_path"] == str(repo))
    assert row["configured"] is False
    assert row["suggestion"]["start_cmd"] == "npm run dev"


async def test_diagnose_no_advisory_once_configured(store, tmp_path):
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    sug = ui_evidence_suggestion(prof, repo)
    apply_ui_evidence_suggestion(prof, sug)
    await persist_profile(store, prof)

    d = await diagnose(store)
    assert not any("VISUAL-PROOF WALKS NOT CONFIGURED" in a for a in d.advisories)
    row = next(r for r in d.ui_evidence if r["repo_path"] == str(repo))
    assert row["configured"] is True
    assert row["suggestion"] is None


# --------------------------------------------------------------------------- #
# API — POST /api/onboarding/repos/ui-evidence: enable / decline / 404 / 422  #
# --------------------------------------------------------------------------- #


async def test_api_ui_evidence_enable(client, store, tmp_path):
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    await store.upsert_profile(prof)

    r = await client.post("/api/onboarding/repos/ui-evidence",
                           json={"repo_path": str(repo), "enabled": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["enabled"] is True
    assert body["ui_evidence"]["start_cmd"] == "npm run dev"
    assert body["ui_evidence"]["base_url"] == "http://localhost:5173"

    from_db = await store.get_profile(str(repo))
    assert from_db.ui_evidence["enabled"] is True
    assert (repo / PROFILE_RELPATH).exists()  # dual write reached disk too


async def test_api_ui_evidence_decline_writes_nothing(client, store, tmp_path):
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    await store.upsert_profile(prof)

    r = await client.post("/api/onboarding/repos/ui-evidence",
                           json={"repo_path": str(repo), "enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False
    from_db = await store.get_profile(str(repo))
    assert from_db.ui_evidence["enabled"] is False
    assert not (repo / PROFILE_RELPATH).exists()


async def test_api_ui_evidence_yml_failure_500s_and_writes_neither_artifact(
    client, store, tmp_path, monkeypatch
):
    """The endpoint must not answer {"ok": True, "enabled": True} when
    project.yml could not be written — that would tell the wizard both
    artifacts match when only (at most) one of them does."""
    repo = _vite_repo(tmp_path / "web")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    await store.upsert_profile(prof)

    monkeypatch.setattr(ProjectProfile, "save", lambda self: (_ for _ in ()).throw(OSError("nope")))

    r = await client.post("/api/onboarding/repos/ui-evidence",
                           json={"repo_path": str(repo), "enabled": True})
    assert r.status_code == 500
    from_db = await store.get_profile(str(repo))
    assert from_db.ui_evidence.get("enabled") is not True
    assert not (repo / PROFILE_RELPATH).exists()


async def test_api_ui_evidence_404_without_profile(client, tmp_path):
    repo = _vite_repo(tmp_path / "web")
    r = await client.post("/api/onboarding/repos/ui-evidence",
                           json={"repo_path": str(repo), "enabled": True})
    assert r.status_code == 404


async def test_api_ui_evidence_422_when_nothing_to_enable(client, store, tmp_path):
    """No dev script detected -> accepting still 422s: there is nothing to
    turn on, and the server must never fabricate a start_cmd/base_url."""
    repo = _plain_node_repo(tmp_path / "svc")
    prof = ProjectProfile(repo_path=str(repo), ecosystem="node")
    await store.upsert_profile(prof)

    r = await client.post("/api/onboarding/repos/ui-evidence",
                           json={"repo_path": str(repo), "enabled": True})
    assert r.status_code == 422


async def test_api_onboard_repo_response_carries_suggestion(client, tmp_path):
    """The onboarding wizard's own onboard-repo call surfaces the same
    suggestion object doctor and the CLI use, so the UI can render the single
    Enable/Not-now row without a second round trip."""
    repo = _vite_repo(tmp_path / "web")
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ui_evidence"]["configured"] is False
    assert body["ui_evidence"]["suggestion"]["start_cmd"] == "npm run dev"


async def test_api_onboard_repo_no_suggestion_without_dev_script(client, tmp_path):
    repo = _plain_node_repo(tmp_path / "svc")
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ui_evidence"]["suggestion"] is None


async def test_api_reonboard_carries_forward_accepted_ui_evidence(client, store, tmp_path):
    """Re-deriving a profile (a repeat wizard run) must not silently wipe a
    previously-accepted ui_evidence config — the carry-forward fix in
    onboarding_onboard_repo."""
    repo = _vite_repo(tmp_path / "web")
    r = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text

    en = await client.post("/api/onboarding/repos/ui-evidence",
                            json={"repo_path": str(repo), "enabled": True})
    assert en.status_code == 200, en.text

    r2 = await client.post("/api/onboarding/repos/onboard", json={"repo_path": str(repo)})
    assert r2.status_code == 200, r2.text
    assert r2.json()["ui_evidence"]["enabled"] is True
    from_db = await store.get_profile(str(repo))
    assert from_db.ui_evidence["enabled"] is True
    assert from_db.ui_evidence["start_cmd"] == "npm run dev"
