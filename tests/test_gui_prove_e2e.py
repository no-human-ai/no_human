"""End-to-end: a repo onboarded ENTIRELY through the web API ends up with a
profile the orchestrator will actually use — and a repo whose test command
FAILS does not.

These are not "the endpoint returns 200" tests. Each one drives the real HTTP
surface against a real git repo on disk, runs a real subprocess as the proof,
and then asserts the property that matters using the SAME predicates the
product uses at runtime:

  * ``ProjectProfile.is_usable``            (profile.py)
  * ``Orchestrator._profile_usable_under_policy`` (core/orchestrator.py)

The failing-command case is the one that gives the passing case its meaning: if
a red suite could still produce a usable profile, "proven" would be decoration.
"""
from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import no_human.config as nh_config
from no_human.api.app import app
from no_human.profile import ProjectProfile


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def client(store, tmp_path, monkeypatch):
    app.state.store = store
    app.state.config = types.SimpleNamespace(
        data={"git": {"github_hosts": ["github.com"]}}
    )
    monkeypatch.setattr(nh_config, "CONFIG_PATH", tmp_path / "config.yaml")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as c:
        yield c


def _git_repo(root, *, test_passes: bool):
    """A real git repo whose declared test command really passes/fails.

    Deliberately NOT pytest: the command is `<this python> run_tests.py`, whose
    exit status is decided by the file on disk. That keeps the proof honest (a
    real subprocess, a real exit code) without nesting a pytest run inside this
    one.
    """
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "run_tests.py").write_text(
        "import sys\n"
        "print('collected 1 item')\n"
        "print('1 passed' if %s else '1 failed')\n"
        "sys.exit(%s)\n" % (test_passes, 0 if test_passes else 1)
    )
    # A Makefile `test` target is a declaration DeclarationDeriver reads, so the
    # command under test is one the product itself derived — not one we injected.
    (root / "Makefile").write_text(
        "test:\n\t%s run_tests.py\n" % sys.executable
    )
    return root


async def _prove(client, repo, **body):
    """Drive POST /api/onboarding/repos/prove and collect the SSE frames."""
    payload = {"repo_path": str(repo), "timeout": 120, **body}
    frames: list[dict] = []
    async with client.stream(
        "POST", "/api/onboarding/repos/prove", json=payload
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[6:]))
    return frames


def _orchestrator_would_accept(prof, *, auto_confirm_proven=False):
    """The orchestrator's OWN gate, called directly — not a copy of it.

    ``Orchestrator._profile_usable_under_policy`` reads only ``self.config``, so
    it is exercised here unbound against a minimal stand-in. If that predicate
    ever changes, this test changes with it, which is the point.
    """
    from no_human.core.orchestrator import Orchestrator
    fake = types.SimpleNamespace(
        config={"profile": {"auto_confirm_proven": auto_confirm_proven}}
    )
    return Orchestrator._profile_usable_under_policy(fake, prof)


# --------------------------------------------------------------------------- #
# The happy path: GUI-only onboarding produces a usable profile                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gui_only_flow_yields_a_profile_the_orchestrator_accepts(
    client, store, tmp_path
):
    repo = _git_repo(tmp_path / "green", test_passes=True)

    # 1. Derive (the wizard's repos step) — unproven, as before.
    r = await client.post("/api/onboarding/repos/onboard",
                          json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    assert r.json()["proven"] is False
    derived = await store.get_profile(str(repo))
    assert derived.is_usable is False, "a derived-only profile must not be usable"

    # 2. Prove — a REAL subprocess, streamed.
    frames = await _prove(client, repo)
    kinds = [f["kind"] for f in frames]
    assert "prove_start" in kinds, kinds
    assert "output" in kinds, "the real command output must be streamed, not hidden"
    done = [f for f in frames if f["kind"] == "done"]
    assert done, f"stream never reported a verdict: {kinds}"
    done = done[0]
    assert done["test_proven"] is True, done
    assert done["test_cmd"], done
    # Still NOT usable: proving is not confirming.
    assert done["is_usable"] is False
    assert done["confirmed"] is False

    proven_cmd = done["test_cmd"]

    # 3. Confirm (the human gate, from the app).
    r = await client.post("/api/onboarding/repos/confirm",
                          json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text
    assert r.json()["is_usable"] is True

    # --- the assertions that matter --------------------------------------- #
    prof = await store.get_profile(str(repo))
    assert isinstance(prof, ProjectProfile)
    assert prof.is_usable is True, (
        "a repo onboarded entirely through the GUI must satisfy "
        "ProjectProfile.is_usable"
    )
    assert _orchestrator_would_accept(prof) is True, (
        "the orchestrator's own gate must accept a GUI-onboarded profile"
    )
    # The proven command is the command that will later run.
    assert prof.test_cmd == proven_cmd
    assert prof.proven.get("test_cmd") is True

    # And it is visible as usable on the surfaces the UI reads.
    r = await client.get("/api/onboarding/readiness")
    assert r.json()["usable"] == 1
    assert r.json()["needs_proving"] == []
    assert r.json()["first_usable"] == str(repo)
    rows = (await client.get("/api/profiles")).json()
    assert [row["is_usable"] for row in rows] == [True]


# --------------------------------------------------------------------------- #
# The property that makes the proof mean anything                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failing_test_command_never_becomes_usable(client, store, tmp_path):
    repo = _git_repo(tmp_path / "red", test_passes=False)

    await client.post("/api/onboarding/repos/onboard",
                      json={"repo_path": str(repo)})
    frames = await _prove(client, repo)
    done = [f for f in frames if f["kind"] == "done"]
    assert done, [f["kind"] for f in frames]
    done = done[0]

    assert done["test_proven"] is False, "a red suite must not be reported proven"
    assert done["is_usable"] is False

    # The failure is SHOWN, not swallowed — the user can act on it.
    results = [f for f in frames if f["kind"] == "prove_result"
               and f["cmd_kind"] == "test"]
    assert results and results[-1]["ok"] is False, results
    assert any(f["kind"] == "output" for f in frames), (
        "a failing run must still stream its output so the user sees why"
    )

    # Confirming is REFUSED — the GUI cannot mint trust the CLI would deny.
    r = await client.post("/api/onboarding/repos/confirm",
                          json={"repo_path": str(repo)})
    assert r.status_code == 422, r.text
    assert "not proven" in r.text

    prof = await store.get_profile(str(repo))
    assert prof.is_usable is False
    assert _orchestrator_would_accept(prof) is False
    # …and not even the opt-in policy that skips the human click accepts it,
    # because that policy still requires a real proof.
    assert _orchestrator_would_accept(prof, auto_confirm_proven=True) is False

    r = await client.get("/api/onboarding/readiness")
    body = r.json()
    assert body["usable"] == 0
    assert [x["repo_path"] for x in body["needs_proving"]] == [str(repo)]


# --------------------------------------------------------------------------- #
# The slow/failing case must not dead-end: edit the command and retry          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_operator_can_correct_a_failing_command_and_retry(
    client, store, tmp_path
):
    """A repo whose DERIVED command fails, but where the human knows the right
    one. The corrected string must be proven byte-for-byte — not "fixed up"."""
    repo = _git_repo(tmp_path / "correctable", test_passes=False)
    # A second, genuinely-passing entry point the deriver does not know about.
    (repo / "good_tests.py").write_text("print('1 passed')\n")

    await client.post("/api/onboarding/repos/onboard",
                      json={"repo_path": str(repo)})

    # First attempt: derived command fails.
    first = await _prove(client, repo)
    assert [f for f in first if f["kind"] == "done"][0]["test_proven"] is False

    # Retry with the human's command.
    corrected = f"{sys.executable} good_tests.py"
    second = await _prove(client, repo, test_cmd=corrected)
    done = [f for f in second if f["kind"] == "done"][0]
    assert done["test_proven"] is True, done
    assert done["test_cmd"] == corrected, (
        "the command recorded as proven must be the exact string the human gave"
    )

    r = await client.post("/api/onboarding/repos/confirm",
                          json={"repo_path": str(repo)})
    assert r.status_code == 200, r.text

    prof = await store.get_profile(str(repo))
    assert prof.is_usable is True
    assert prof.test_cmd == corrected
    assert _orchestrator_would_accept(prof) is True


@pytest.mark.asyncio
async def test_reproving_a_confirmed_profile_drops_the_confirm(
    client, store, tmp_path
):
    """Re-proving must not inherit an old human confirm: the command may have
    changed, so the human confirms against THIS evidence or not at all."""
    repo = _git_repo(tmp_path / "reprove", test_passes=True)
    await client.post("/api/onboarding/repos/onboard",
                      json={"repo_path": str(repo)})
    await _prove(client, repo)
    await client.post("/api/onboarding/repos/confirm", json={"repo_path": str(repo)})
    assert (await store.get_profile(str(repo))).is_usable is True

    await _prove(client, repo)
    prof = await store.get_profile(str(repo))
    assert prof.confirmed is False
    assert prof.is_usable is False, "a re-proved profile must be re-confirmed"


# --------------------------------------------------------------------------- #
# Streaming behaviour                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_streamed_output_is_the_real_command_output(client, tmp_path):
    """"Stream progress, never a spinner with no output" — assert the bytes the
    subprocess actually printed reach the client."""
    repo = _git_repo(tmp_path / "chatty", test_passes=True)
    await client.post("/api/onboarding/repos/onboard",
                      json={"repo_path": str(repo)})
    frames = await _prove(client, repo)
    lines = [f["line"] for f in frames if f["kind"] == "output"]
    assert any("collected 1 item" in ln for ln in lines), lines
    assert any("1 passed" in ln for ln in lines), lines


@pytest.mark.asyncio
async def test_prove_rejects_a_non_repo(client, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    r = await client.post("/api/onboarding/repos/prove",
                          json={"repo_path": str(plain)})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# The shared confirm gate                                                      #
# --------------------------------------------------------------------------- #


def test_confirm_profile_refuses_an_unproven_profile():
    """The gate the CLI and the API both call. If this ever returns instead of
    raising, every GUI confirm silently mints trust."""
    from no_human.onboard import ProfileNotProven, confirm_profile

    prof = ProjectProfile(repo_path="/tmp/x", test_cmd="pytest -q", proven={})
    with pytest.raises(ProfileNotProven):
        confirm_profile(prof)
    assert prof.confirmed is False

    prof.proven = {"test_cmd": True}
    assert confirm_profile(prof).confirmed is True
    assert prof.is_usable is True


def test_confirm_profile_refuses_when_there_is_no_test_command():
    from no_human.onboard import ProfileNotProven, confirm_profile

    prof = ProjectProfile(repo_path="/tmp/x", test_cmd="", proven={"test_cmd": True})
    with pytest.raises(ProfileNotProven):
        confirm_profile(prof)
    assert prof.confirmed is False
